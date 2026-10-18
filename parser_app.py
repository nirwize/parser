import os
import re
import sys
import time
import queue
import ctypes
import threading
import tkinter as tk
import customtkinter as ctk
from PIL import Image
from playwright.sync_api import sync_playwright

PHONE_RE = re.compile(r'(?:\+7|8)[\s\-\(]*\d{3}[\s\-\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}')
SOCIAL_DOMAINS = ("vk.com", "vk.ru", "instagram.com", "t.me", "wa.me", "ok.ru", "facebook.com", "youtube.com")
SNIPPET = '.search-snippet-view'

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")


def res(name):
    """Путь к файлу, вшитому в exe (или лежащему рядом со скриптом)."""
    base = getattr(sys, "_MEIPASS", BASE_DIR)
    return os.path.join(base, name)


# --- оформление ---
BG = "#000000"        # фон окна и заголовка (полностью чёрный)
BORDER = "#141414"    # рамки полей — цвет как на скриншоте заголовка
HOVER = "#141414"
TEXT = "#e8e8e8"
MUTED = "#6b6b6b"


def load_more(page, need_index, stop):
    stale = 0
    while not stop.is_set():
        snippets = page.query_selector_all(SNIPPET)
        if len(snippets) > need_index:
            return True
        if not snippets:
            return False
        before = len(snippets)
        try:
            snippets[-1].scroll_into_view_if_needed()
        except Exception:
            pass
        page.mouse.move(200, 500)
        page.mouse.wheel(0, 3000)
        time.sleep(1.5)
        after = len(page.query_selector_all(SNIPPET))
        stale = stale + 1 if after <= before else 0
        if stale >= 4:
            return False
    return False


def back_to_list(page):
    btn = page.query_selector('[class*="close-button"]')
    if btn:
        try:
            btn.click()
            time.sleep(1)
            return
        except Exception:
            pass
    page.go_back()
    time.sleep(1.5)


def launch_browser(p, log):
    args = ["--disable-blink-features=AutomationControlled"]
    for channel in ("chrome", "msedge"):
        try:
            b = p.chromium.launch(channel=channel, headless=False, args=args)
            log(f"Браузер: {channel}")
            return b
        except Exception:
            continue
    log("Chrome/Edge не найдены, пробую встроенный Chromium...")
    return p.chromium.launch(headless=False, args=args)


def wait_for_list(page, log, stop, timeout=300):
    """Ждёт список карточек. Если капча — даёт время её решить."""
    start = time.time()
    warned = False
    while time.time() - start < timeout and not stop.is_set():
        try:
            page.wait_for_selector(SNIPPET, timeout=3000)
            return True
        except Exception:
            if not warned:
                log("Список не появился. Если видишь капчу — реши её в окне браузера, я подожду (до 5 минут).")
                warned = True
    return False


def parse(query, city, target, filename, log, stop):
    found = 0
    seen = set()
    search_text = f"{query} {city}"
    log(f"Ищу: {search_text} (нужно {target} без сайта и с телефоном)")

    with open(filename, "w", encoding="utf-8") as f, sync_playwright() as p:
        browser = launch_browser(p, log)
        context = browser.new_context(
            storage_state=STATE_FILE if os.path.exists(STATE_FILE) else None,
            viewport={"width": 1400, "height": 900},
            locale="ru-RU",
        )
        page = context.new_page()
        page.goto(f"https://yandex.ru/maps/?text={search_text.replace(' ', '+')}")

        if not wait_for_list(page, log, stop):
            log("Список так и не загрузился. Проверь запрос или капчу и запусти снова.")
            browser.close()
            return 0

        try:
            context.storage_state(path=STATE_FILE)
        except Exception:
            pass

        i = 0
        while found < target and not stop.is_set():
            try:
                if not load_more(page, i, stop):
                    log("Список закончился.")
                    break

                snippets = page.query_selector_all(SNIPPET)
                snippets[i].scroll_into_view_if_needed()
                snippets[i].click()
                time.sleep(2)

                btn = page.query_selector('[class*="phones-view__more"]')
                if btn:
                    btn.click()
                    time.sleep(0.7)

                panel = page.query_selector('.business-card-view') or page.query_selector('body')
                text = panel.inner_text()

                name_el = panel.query_selector('h1') or panel.query_selector('[class*="header-view__header"]')
                name = name_el.inner_text().strip() if name_el else ""

                has_site = False
                for a in panel.query_selector_all('[class*="business-urls-view__link"]'):
                    href = (a.get_attribute("href") or "").lower()
                    if any(d in href for d in SOCIAL_DOMAINS):
                        continue
                    has_site = True
                    break

                phones = list(dict.fromkeys(m.strip() for m in PHONE_RE.findall(text)))

                if not name or name in seen:
                    pass
                elif has_site:
                    log(f"⏭ {name} — есть сайт")
                elif not phones:
                    log(f"⏭ {name} — нет телефона")
                else:
                    seen.add(name)
                    found += 1
                    line = f"{name} | {', '.join(phones)}"
                    f.write(line + "\n")
                    f.flush()
                    log(f"[{found}/{target}] {line}")
            except Exception as e:
                log(f"Карточка {i}: {e}")
            finally:
                i += 1
                try:
                    back_to_list(page)
                except Exception:
                    pass

        browser.close()
    return found


class App(ctk.CTk):
    W, H = 600, 560

    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.configure(fg_color=BG)
        self.title("Парсер Яндекс Карт")
        self.geometry(f"{self.W}x{self.H}")
        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_width()) // 2
        y = (self.winfo_screenheight() - self.winfo_height()) // 2
        self._set_geo(f"+{x}+{y}")

        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self._maxed = False
        self._minimized = False
        self._normal_geo = None

        # окно без системной рамки, свой чёрный заголовок
        self.overrideredirect(True)
        self.after(300, self._set_icon)
        self.after(50, self._fix_taskbar)
        self.bind("<Map>", self._on_map, add="+")

        frame = ctk.CTkFrame(self, fg_color=BG, border_width=1, border_color=BORDER, corner_radius=0)
        frame.pack(fill="both", expand=True)

        # ---- заголовок ----
        bar = ctk.CTkFrame(frame, fg_color=BG, height=34, corner_radius=0)
        bar.pack(fill="x", padx=1, pady=(1, 0))
        bar.pack_propagate(False)

        try:
            logo_img = ctk.CTkImage(Image.open(res("sparkle.png")), size=(18, 18))
            logo = ctk.CTkLabel(bar, image=logo_img, text="", fg_color=BG)
            logo.pack(side="left", padx=(12, 8))
        except Exception:
            logo = None
        title = ctk.CTkLabel(bar, text="Парсер Яндекс Карт", text_color=MUTED, fg_color=BG,
                             font=ctk.CTkFont(size=13))
        title.pack(side="left")

        def ctl(text, cmd, size=15):
            return ctk.CTkButton(bar, text=text, width=46, height=34, corner_radius=0,
                                 fg_color=BG, hover_color=HOVER, text_color=TEXT,
                                 font=ctk.CTkFont(size=size), command=cmd)
        ctl("✕", self.destroy, 14).pack(side="right")
        ctl("☐", self.toggle_max, 15).pack(side="right")
        ctl("—", self.minimize, 15).pack(side="right")

        for w in (bar, title) + ((logo,) if logo else ()):
            w.bind("<ButtonPress-1>", self._start_move)
            w.bind("<B1-Motion>", self._do_move)
            w.bind("<Double-Button-1>", lambda e: self.toggle_max())

        # ---- содержимое ----
        body = ctk.CTkFrame(frame, fg_color=BG, corner_radius=0)
        body.pack(fill="both", expand=True, padx=20, pady=(14, 20))

        def entry(placeholder):
            return ctk.CTkEntry(body, placeholder_text=placeholder, height=40, corner_radius=8,
                                fg_color=BG, border_color=BORDER, border_width=1,
                                text_color=TEXT, placeholder_text_color=MUTED)

        self.niche = entry("Ниша (например: Детейлинг)")
        self.niche.pack(fill="x", pady=(0, 10))
        self.city = entry("Город (например: Уфа)")
        self.city.pack(fill="x", pady=10)
        self.target = entry("Сколько контактов нужно (например: 50)")
        self.target.pack(fill="x", pady=10)

        self.btn = ctk.CTkButton(body, text="Старт", height=42, corner_radius=8,
                                 fg_color=BORDER, hover_color="#222222", text_color=TEXT,
                                 command=self.toggle)
        self.btn.pack(fill="x", pady=10)

        self.logbox = ctk.CTkTextbox(body, fg_color=BG, border_width=1, border_color=BORDER,
                                     text_color="#c8c8c8", corner_radius=8,
                                     scrollbar_button_color=BORDER, scrollbar_button_hover_color="#2a2a2a")
        self.logbox.pack(fill="both", expand=True, pady=(10, 0))

        self.after(200, self.poll)

    # ---------- работа с окном ----------
    def _set_geo(self, s):
        tk.Tk.geometry(self, s)  # в обход масштабирования customtkinter

    def _set_icon(self):
        try:
            self.iconbitmap(res("icon.ico"))
        except Exception:
            pass

    def _fix_taskbar(self):
        """Чтобы окно без рамки всё равно было видно на панели задач."""
        if sys.platform != "win32":
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            style = (style & ~0x00000080) | 0x00040000
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, style)
            self.withdraw()
            self.after(10, self.deiconify)
        except Exception:
            pass

    def minimize(self):
        self._minimized = True
        self.overrideredirect(False)
        self.iconify()

    def _on_map(self, event):
        if event.widget is self and self._minimized:
            self._minimized = False
            self.after(10, self._restore_frameless)

    def _restore_frameless(self):
        self.overrideredirect(True)
        self._fix_taskbar()

    def _work_area(self):
        try:
            class RECT(ctypes.Structure):
                _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                            ("r", ctypes.c_long), ("b", ctypes.c_long)]
            r = RECT()
            ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(r), 0)
            return r.l, r.t, r.r, r.b
        except Exception:
            return 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()

    def toggle_max(self):
        if self._maxed:
            self._set_geo(self._normal_geo)
            self._maxed = False
        else:
            self._normal_geo = tk.Tk.geometry(self)
            l, t, r, b = self._work_area()
            self._set_geo(f"{r - l}x{b - t}+{l}+{t}")
            self._maxed = True

    def _start_move(self, e):
        self._dx = e.x_root - self.winfo_x()
        self._dy = e.y_root - self.winfo_y()

    def _do_move(self, e):
        if self._maxed:
            return
        self._set_geo(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    # ---------- логика ----------

    def log(self, msg):
        self.q.put(msg)

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg == "__DONE__":
                    self.btn.configure(text="Старт")
                else:
                    self.logbox.insert("end", msg + "\n")
                    self.logbox.see("end")
        except queue.Empty:
            pass
        self.after(200, self.poll)

    def toggle(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.log("Останавливаю...")
            return

        niche = self.niche.get().strip()
        city = self.city.get().strip()
        try:
            target = int(self.target.get().strip())
        except ValueError:
            self.log("Поле «Сколько нужно» должно быть числом.")
            return
        if not niche or not city or target <= 0:
            self.log("Заполни все три поля.")
            return

        filename = os.path.join(BASE_DIR, f"{niche}_{city}.txt")
        self.stop_event.clear()
        self.btn.configure(text="Стоп")

        def run():
            try:
                count = parse(niche, city, target, filename, self.log, self.stop_event)
                self.log(f"Готово! Собрано {count} контактов.")
                self.log(f"Файл: {filename}")
            except Exception as e:
                self.log(f"Ошибка: {e}")
            finally:
                self.log("__DONE__")

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()


if __name__ == "__main__":
    App().mainloop()
