"""Run the window by default; --cli for console use."""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import webbrowser
from dataclasses import asdict, replace
from pathlib import Path

from olx_parser.browser import Scanner
from olx_parser.core import Config, Listing, REASONS, ROOT


def run_cli(config: Config, output: Path | None) -> int:
    stop = threading.Event()
    sink = None
    try:
        if output:
            # Exclusive creation protects earlier exports.
            sink = output.open("x", encoding="utf-8")
        def result(item: Listing):
            line = json.dumps(asdict(item), ensure_ascii=False)
            print(line, flush=True)
            if sink:
                sink.write(line + "\n")
                sink.flush()
        import signal
        previous = signal.signal(signal.SIGINT, lambda *_: stop.set())
        try:
            summary = Scanner(config, stop, log=lambda text: print(text, file=sys.stderr), on_result=result).run()
        finally:
            signal.signal(signal.SIGINT, previous)
        print(f"{summary.message} Проверено: {summary.checked}; найдено: {summary.accepted}.", file=sys.stderr)
        for reason, count in summary.reasons.items():
            print(f"  {REASONS[reason]}: {count}", file=sys.stderr)
        return 0 if summary.status == "complete" else 130 if summary.status == "stopped" else 1
    finally:
        if sink:
            sink.close()


def run_gui(config_path: Path, initial_config: Config):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("OLX Portugal — поиск товаров")
            self.geometry("1000x680")
            self.minsize(760, 500)
            self.configure(bg="#15171b")
            self.events = queue.Queue()
            self.stop = threading.Event()
            self.worker = None
            self.closing = False
            self.results: list[Listing] = []
            style = ttk.Style(self)
            style.theme_use("clam")
            style.configure("TFrame", background="#15171b")
            style.configure("TLabel", background="#15171b", foreground="#e8e8e8", font=("Segoe UI", 10))
            style.configure("Treeview", background="#20242b", fieldbackground="#20242b", foreground="#e8e8e8", rowheight=28)
            style.map("Treeview", background=[("selected", "#175c63")])
            body = ttk.Frame(self, padding=18)
            body.pack(fill="both", expand=True)
            ttk.Label(body, text="OLX Portugal", font=("Segoe UI", 20, "bold")).pack(anchor="w")
            ttk.Label(body, text="Публичные признаки проверяются временно. В результатах — только товары и ссылки.").pack(anchor="w", pady=(3, 14))
            controls = ttk.Frame(body)
            controls.pack(fill="x")
            ttk.Label(controls, text="Нужно объявлений:").pack(side="left")
            self.target = tk.StringVar(value=str(initial_config.target))
            ttk.Entry(controls, textvariable=self.target, width=8).pack(side="left", padx=8)
            self.start_button = ttk.Button(controls, text="Начать поиск", command=self.start)
            self.start_button.pack(side="left", padx=4)
            self.stop_button = ttk.Button(controls, text="Стоп", command=self.cancel, state="disabled")
            self.stop_button.pack(side="left", padx=4)
            ttk.Button(controls, text="Сохранить результаты…", command=self.save).pack(side="right")
            self.status = tk.StringVar(value="Категории и фильтры задаются в config.json. Двойной щелчок открывает объявление.")
            ttk.Label(body, textvariable=self.status, wraplength=930).pack(fill="x", pady=12)
            self.tree = ttk.Treeview(body, columns=("title", "price", "category"), show="headings", selectmode="browse")
            self.tree.heading("title", text="Товар")
            self.tree.heading("price", text="Цена, €")
            self.tree.heading("category", text="Категория")
            self.tree.column("title", width=520)
            self.tree.column("price", width=100, anchor="e")
            self.tree.column("category", width=210)
            self.tree.pack(fill="both", expand=True)
            self.tree.bind("<Double-1>", self.open_result)
            self.tree.bind("<Return>", self.open_result)
            ttk.Label(body, text="Ход проверки").pack(anchor="w", pady=(12, 4))
            self.logs = tk.Text(body, height=8, bg="#20242b", fg="#c5c9d1", relief="flat", wrap="word", state="disabled")
            self.logs.pack(fill="x")
            self.protocol("WM_DELETE_WINDOW", self.close)
            self.after(100, self.poll)

        def append_log(self, text):
            self.logs.configure(state="normal")
            self.logs.insert("end", text + "\n")
            # Keep the UI bounded for long runs. No log file is created.
            if int(self.logs.index("end-1c").split(".")[0]) > 500:
                self.logs.delete("1.0", "101.0")
            self.logs.see("end")
            self.logs.configure(state="disabled")

        def start(self):
            if self.worker and self.worker.is_alive():
                return
            try:
                count = int(self.target.get())
                if not 1 <= count <= 10000:
                    raise ValueError("Количество должно быть от 1 до 10000.")
                config = replace(Config.load(config_path), target=count)
            except ValueError as exc:
                messagebox.showerror("Настройки", str(exc), parent=self)
                return
            self.results.clear()
            for item in self.tree.get_children():
                self.tree.delete(item)
            self.stop.clear()
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self.status.set("Запускается браузер…")
            self.append_log("Новый запуск. Настройки прочитаны из config.json.")
            def work():
                scanner = Scanner(config, self.stop,
                                  log=lambda text: self.events.put(("log", text)),
                                  on_result=lambda item: self.events.put(("result", item)),
                                  on_progress=lambda checked, accepted: self.events.put(("progress", (checked, accepted))))
                self.events.put(("done", scanner.run()))
            self.worker = threading.Thread(target=work, name="olx-parser", daemon=False)
            self.worker.start()

        def cancel(self):
            self.stop.set()
            self.stop_button.configure(state="disabled")
            self.status.set("Останавливается сбор и закрывается браузер…")

        def poll(self):
            try:
                while True:
                    kind, value = self.events.get_nowait()
                    if kind == "log":
                        self.append_log(value)
                    elif kind == "result":
                        index = len(self.results)
                        self.results.append(value)
                        self.tree.insert("", "end", iid=str(index), values=(value.title, value.price_eur, value.category))
                    elif kind == "progress":
                        self.status.set(f"Проверено: {value[0]} · Подходит: {value[1]}")
                    elif kind == "done":
                        self.status.set(f"{value.message} Проверено: {value.checked}; найдено: {value.accepted}.")
                        self.append_log(value.message)
                        for reason, count in value.reasons.items():
                            self.append_log(f"{REASONS[reason]}: {count}")
                        self.start_button.configure(state="normal")
                        self.stop_button.configure(state="disabled")
            except queue.Empty:
                pass
            if self.closing and (not self.worker or not self.worker.is_alive()):
                self.destroy()
                return
            self.after(100, self.poll)

        def open_result(self, _event=None):
            selected = self.tree.selection()
            if selected:
                webbrowser.open(self.results[int(selected[0])].url)

        def save(self):
            if not self.results:
                messagebox.showinfo("Результаты", "Пока нет подходящих объявлений.", parent=self)
                return
            path = filedialog.asksaveasfilename(parent=self, defaultextension=".json", filetypes=[("JSON", "*.json")], initialfile="olx_goods.json")
            if path:
                try:
                    Path(path).write_text(json.dumps([asdict(item) for item in self.results], ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError:
                    messagebox.showerror("Сохранение", "Не удалось записать выбранный файл.", parent=self)

        def close(self):
            self.closing = True
            self.start_button.configure(state="disabled")
            self.cancel()

    App().mainloop()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Фильтр публичных объявлений OLX.pt без сохранения сведений о продавцах.")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--cli", action="store_true", help="Консольный режим вместо окна")
    parser.add_argument("--check-config", action="store_true", help="Проверить настройки без браузера")
    parser.add_argument("--target", type=int, help="Нужное количество объявлений")
    parser.add_argument("--max-ads", type=int, help="Лимит проверяемых объявлений")
    parser.add_argument("--headless", action="store_true", help="Скрытый браузер в консольном режиме")
    parser.add_argument("--output", type=Path, help="Явно сохранить результаты CLI в новый JSONL-файл")
    args = parser.parse_args()
    try:
        config = Config.load(args.config)
        if args.check_config:
            print(f"Настройки корректны. Включено категорий: {sum(c.enabled for c in config.categories)}.")
            return 0
        if not args.cli and (args.output or args.headless or args.max_ads is not None):
            parser.error("--output, --headless и --max-ads используются вместе с --cli")
        if args.target is not None:
            if not 1 <= args.target <= 10000:
                parser.error("--target: допустимо 1–10000")
            config = replace(config, target=args.target)
        if args.max_ads is not None:
            if not 1 <= args.max_ads <= 100000:
                parser.error("--max-ads: допустимо 1–100000")
            config = replace(config, max_ads_per_run=args.max_ads)
        if args.headless:
            config = replace(config, headless=True)
        if args.cli:
            return run_cli(config, args.output)
        run_gui(args.config, config)
        return 0
    except (ValueError, OSError) as exc:
        # These errors originate in local config/files, never in scraped page text.
        print(f"Ошибка настроек или файла: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

