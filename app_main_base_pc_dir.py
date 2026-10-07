# HVOS PC Overlay (Base Edition) v0.7 / 2026-10-07
# 音声なし版。API キーは config.json から読みます（初回起動時に入力）。
import ctypes
import io
import json
import os
import sys
import threading
import tkinter as tk

import keyboard
import pyautogui
from google import genai
from PIL import Image

# ==========================================
# 0. コンソール出力のUTF-8化（ASCIIエラー対策）
# ==========================================
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

# プログラムと同じフォルダに設定ファイルを置く（管理者実行でも場所がずれない）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_CONFIG_FILE = os.path.join(BASE_DIR, "config.json")            # APIキー（他人に渡さない）
WINDOW_CONFIG_FILE = os.path.join(BASE_DIR, "window_config.json")  # 配置・モデル選択

# ==========================================
# モデル設定（二択）
# ==========================================
AVAILABLE_MODELS = [
    "gemini-3.5-flash-lite",  # デフォルト（高速）
    "gemini-3.6-flash",       # 標準（混雑時に 503 が出ることがあります）
]
DEFAULT_MODEL = AVAILABLE_MODELS[0]
selected_model = DEFAULT_MODEL

# 画像縮小の上限（長辺ピクセル）。トークン節約と高速化のため
MAX_IMAGE_SIDE = 1280

# ==========================================
# ターミナル画面を最背面へ移動＆フォーカス解除
# ==========================================
if os.name == 'nt':
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd != 0:
        flags = 0x0001 | 0x0002 | 0x0010
        ctypes.windll.user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, flags)


# ==========================================
# 🔑 APIキー読み込み（config.json）
# ==========================================
def load_api_key():
    """config.json からキーを読む。無ければ入力を求めて保存する。"""
    if os.path.exists(API_CONFIG_FILE):
        try:
            with open(API_CONFIG_FILE, 'r', encoding='utf-8') as f:
                key = str(json.load(f).get('api_key', '')).strip()
            if key:
                return key
        except Exception as e:
            print(f"[API] config.json の読み込みに失敗しました: {e}")

    print("==========================================")
    print(" 初回設定: Gemini API キーを貼り付けて Enter を押してください")
    print(" （黒い画面では、右クリックで貼り付けできます）")
    print("==========================================")
    key = input("APIキー: ").strip().strip('"').strip("'").strip()
    if not key:
        print("[API] キーが入力されませんでした。終了します。")
        input("Enter キーで閉じます")
        sys.exit(1)
    try:
        with open(API_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump({'api_key': key}, f, ensure_ascii=False, indent=2)
        print("[API] config.json に保存しました。次回から入力は不要です。")
        print("[API] キーを変えたいときは config.json を削除して、もう一度起動してください。")
    except Exception as e:
        print(f"[API] config.json の保存に失敗しました: {e}")
    return key


GEMINI_API_KEY = load_api_key()
genai_client = genai.Client(api_key=GEMINI_API_KEY)

# グローバル状態管理
target_side = 'left'   # 初期値：画面の左側
is_processing = False  # 重複解析防止フラグ


# ==========================================
# UI (tkinter) オーバーレイ画面の管理クラス
# ==========================================
class OverlayUI:

    def __init__(self):
        global selected_model
        self.root = tk.Tk()
        self.root.title('HVOS PC Direct')

        # 🎨 ウィンドウ表示・透過設定
        self.root.attributes('-topmost', True)
        self.root.attributes('-alpha', 0.85)
        self.root.configure(bg='#1e1e1e')

        # 📐 ウィンドウ位置・サイズ・モデル選択の読み込み
        saved = self.load_window_config()
        self.root.geometry(saved.get('geometry', '600x800+50+50'))
        if saved.get('model') in AVAILABLE_MODELS:
            selected_model = saved['model']

        # --- 1. ステータス表示（ウィンドウ幅に合わせて折り返し） ---
        self.status_label = tk.Label(
            self.root,
            text='[HVOS Direct] ターゲット: 左',
            font=('メイリオ', 8, 'bold'),
            fg='#4CAF50',
            bg='#1e1e1e',
            anchor='w',
            justify='left',
            wraplength=420,
        )
        self.status_label.pack(fill=tk.X, padx=12, pady=(10, 2))

        # --- 2. モデル選択（独立した1行。幅に合わせて伸縮） ---
        model_frame = tk.Frame(self.root, bg='#1e1e1e')
        model_frame.pack(fill=tk.X, padx=10, pady=(0, 6))

        tk.Label(
            model_frame,
            text='モデル',
            font=('メイリオ', 8),
            fg='#94a3b8',
            bg='#1e1e1e',
        ).pack(side=tk.LEFT, padx=(2, 6))

        self.model_var = tk.StringVar(self.root)
        self.model_var.set(selected_model)

        self.model_menu = tk.OptionMenu(
            model_frame,
            self.model_var,
            *AVAILABLE_MODELS,
            command=self.on_model_change
        )
        self.model_menu.config(
            font=('Arial', 8, 'bold'),
            bg='#2d3748',
            fg='#ffffff',
            activebackground='#4a5568',
            activeforeground='#ffffff',
            bd=0,
            highlightthickness=0,
            anchor='w',
            width=1,
        )
        self.model_menu["menu"].config(bg='#2d3748', fg='#ffffff')
        self.model_menu.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ウィンドウ幅の変更に合わせて、ステータスの折り返し幅を調整
        self.root.bind('<Configure>', self.on_resize)

        # --- 3. テキスト表示エリア ---
        frame = tk.Frame(self.root, bg='#1e1e1e')
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.scroll_canvas = tk.Canvas(
            frame, bg='#2d2d2d', width=16, highlightthickness=0, bd=0
        )
        self.scroll_canvas.pack(side=tk.RIGHT, fill=tk.Y)

        self.text_area = tk.Text(
            frame,
            font=('メイリオ', 10),
            fg='#ffffff',
            bg='#2d2d2d',
            wrap=tk.WORD,
            bd=0,
            padx=10,
            pady=10,
            yscrollcommand=self.update_custom_scrollbar,
        )
        self.text_area.pack(fill=tk.BOTH, expand=True)

        self.scroll_canvas.bind('<B1-Motion>', self.on_scroll_drag)
        self.scroll_canvas.bind('<Button-1>', self.on_scroll_click)

        self.text_area.insert(
            tk.END,
            '画面上の解説したいエリア（← / →）を選び、【1】キーを押してください。',
        )
        self.text_area.config(state=tk.DISABLED)

        # ❌ 「×」ボタン押下時の終了イベント登録
        self.root.protocol('WM_DELETE_WINDOW', self.on_closing)

    def on_resize(self, event):
        """幅に合わせてステータス文の折り返し位置を更新する"""
        if event.widget is self.root:
            self.status_label.config(wraplength=max(120, event.width - 30))

    def on_model_change(self, value):
        global selected_model
        selected_model = value
        print(f"[API] 使用モデル変更 ➔ 【{selected_model}】")
        self.save_window_config()

    def load_window_config(self):
        """保存された位置・サイズ・モデル選択を読み込む"""
        if os.path.exists(WINDOW_CONFIG_FILE):
            try:
                with open(WINDOW_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"[画面] 設定読み込み失敗: {e}")
        return {}

    def save_window_config(self):
        """現在の位置・サイズ・モデル選択を保存する"""
        try:
            data = {
                'geometry': self.root.geometry(),
                'model': selected_model,
            }
            with open(WINDOW_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[画面] 設定保存エラー: {e}")

    def on_closing(self):
        """「×」ボタンで安全に終了"""
        print('\n[画面] パネルを閉じて安全に終了します...')
        self.save_window_config()
        self.root.destroy()
        os._exit(0)

    def update_custom_scrollbar(self, first, last):
        self.text_area.yview_moveto(first)
        self.scroll_canvas.delete('all')
        f, l = float(first), float(last)
        if l - f >= 1.0:
            return
        ch = self.scroll_canvas.winfo_height()
        y1 = f * ch
        y2 = l * ch
        self.scroll_canvas.create_rectangle(
            2, y1, 14, y2, fill='#555555', outline=''
        )

    def on_scroll_drag(self, event):
        ch = self.scroll_canvas.winfo_height()
        if ch > 0:
            self.text_area.yview_moveto(event.y / ch)

    def on_scroll_click(self, event):
        ch = self.scroll_canvas.winfo_height()
        if ch > 0:
            self.text_area.yview_moveto(event.y / ch)

    def safe_update_status(self, text, color='#4CAF50'):
        self.root.after(0, lambda: self.status_label.config(text=text, fg=color))

    def safe_update_text(self, text):
        def _update():
            self.text_area.config(state=tk.NORMAL)
            self.text_area.delete('1.0', tk.END)
            self.text_area.insert(tk.END, text)
            self.text_area.config(state=tk.DISABLED)

        self.root.after(0, _update)


ui = None


# ==========================================
# 📸 キャプチャ ＆ Gemini API 呼び出し処理
# ==========================================
def shrink_image(img):
    """長辺が MAX_IMAGE_SIDE を超える場合だけ縮小する（トークン節約・高速化）"""
    w, h = img.size
    longest = max(w, h)
    if longest <= MAX_IMAGE_SIDE:
        return img
    scale = MAX_IMAGE_SIDE / longest
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def capture_and_analyze():
    global is_processing
    if is_processing:
        return
    is_processing = True

    # 使用するモデルを、処理開始時点で確定させる
    model_name = selected_model
    side_label = '左' if target_side == 'left' else '右'

    prompt = (
        'あなたは最高のバーチャルツアーガイドです。この画像に写っている景色・場所について、'
        '以下の構成で500文字程度で魅力的に解説してください。\n\n'
        '1. 【場所の特定と概要】：ここがどこか、何という施設・景色か\n'
        '2. 【歴史と背景】：この場所にまつわる歴史やストーリー、建築のこだわりなど\n'
        '3. 【ここだけの魅力・おすすめポイント】：訪れた人がワクワクする豆知識や見どころ\n'
        '4. 【周囲のおすすめ・楽しみ方】：立ち寄るべき周辺スポットや楽しみ方\n\n'
        '語り口は親しみやすくまとめてください。'
        '文末には必ず『（文字数：〇〇文字）』と実際に生成した文字数を記載してください。'
    )

    ui.safe_update_status(f'📸 {side_label}をスキャン中...', '#FF9800')
    ui.safe_update_text(f'画像を切り出し、Gemini ({model_name}) へ送信中...\nしばらくお待ちください。')

    try:
        screen_width, screen_height = pyautogui.size()
        if target_side == 'left':
            left = int(screen_width * 0.01)
        else:
            left = int(screen_width * 0.51)
        top = int(screen_height * 0.01)
        width = int(screen_width * 0.48)
        height = int(screen_height * 0.98)

        # [画面] 画面取得 → 縮小（ディスクには保存しない）
        print(f"[画面] キャプチャ実行: {side_label}")
        screenshot = pyautogui.screenshot(region=(left, top, width, height))
        img = shrink_image(screenshot)
        print(f"[画面] 送信サイズ: {img.size[0]}x{img.size[1]}")

        ui.safe_update_status(f'🧠 解析中... ({model_name})', '#2196F3')

        # [API] Gemini 呼び出し
        print(f"[API] 呼び出し実行モデル: {model_name}")
        response = genai_client.models.generate_content(
            model=model_name,
            contents=[prompt, img],
            config={'automatic_function_calling': {'disable': True}}
        )

        ui.safe_update_text(response.text)
        ui.safe_update_status(f'✅ 解析完了 ({model_name} / {side_label})', '#4CAF50')
        print("[API] 解析完了")

    except Exception as e:
        print(f"[API] エラー: {e}")
        ui.safe_update_text(f'エラーが発生しました:\n{str(e)}')
        ui.safe_update_status(f'❌ エラー発生 ({model_name})', '#F44336')

    finally:
        is_processing = False


# ==========================================
# ⌨️ キーボードショートカットの監視スレッド
# ==========================================
def listen_keyboard():
    global target_side
    while True:
        if keyboard.is_pressed('left'):
            target_side = 'left'
            ui.safe_update_status('[HVOS Direct] ターゲット: 左', '#4CAF50')
            pyautogui.sleep(0.3)
        elif keyboard.is_pressed('right'):
            target_side = 'right'
            ui.safe_update_status('[HVOS Direct] ターゲット: 右', '#4CAF50')
            pyautogui.sleep(0.3)
        elif keyboard.is_pressed('1'):
            threading.Thread(target=capture_and_analyze, daemon=True).start()
            pyautogui.sleep(0.5)
        pyautogui.sleep(0.05)


# ==========================================
# メイン実行エリア
# ==========================================
if __name__ == '__main__':
    ui = OverlayUI()

    threading.Thread(target=listen_keyboard, daemon=True).start()

    try:
        ui.root.mainloop()
    except KeyboardInterrupt:
        ui.save_window_config()
        print('\n[画面] KeyboardInterrupt を検知。安全に終了します...')
        os._exit(0)