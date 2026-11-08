from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from .core import (Cancelled, LOSSLESS_PRESET, LOSSY_PRESET, MAX_SIZE_BYTES, PROCESSING_ALGORITHM,
                   TARGET_SIZE_BYTES,
                   VideoProcessor, format_bytes, output_size_is_valid)

APP_NAME = "VideoShrink"
VERSION = "1.1.6"
MODE_LOSSLESS = "lossless"
MODE_ADAPTIVE = "adaptive"


def runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_root() -> Path:
    if getattr(sys, "frozen", False) and (runtime_root() / "installed.json").exists():
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            folder_id = GUID(0x18989B1D, 0x99B5, 0x455B,
                             (ctypes.c_ubyte * 8)(0x84, 0xC1, 0xAB, 0x7C, 0x74, 0xE4, 0xDD, 0xFC))
            path = wintypes.LPWSTR()
            result = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(path))
            if result == 0:
                try:
                    return Path(path.value) / APP_NAME
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(path)
        except (AttributeError, OSError, TypeError):
            pass
        return Path.home() / "Videos" / APP_NAME
    portable_marker = runtime_root() / "portable.json"
    if getattr(sys, "frozen", False) and portable_marker.exists():
        try:
            (runtime_root() / "Input").mkdir(exist_ok=True)
            return runtime_root()
        except OSError:
            pass
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME


def ensure_layout(root: Path) -> dict[str, Path]:
    folders = {key: root / key for key in ("Input", "Output", "Logs", "Temp")}
    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)
    return folders


def load_config(root: Path) -> dict:
    config_path = root / "config.json"
    defaults = {"input_dir": "Input", "output_dir": "Output", "temp_dir": "Temp",
                "codec": "h264", "quality": "compatibility", "lossless": False,
                "compression_mode": MODE_LOSSLESS,
                "max_size_bytes": MAX_SIZE_BYTES, "target_size_bytes": TARGET_SIZE_BYTES,
                "quality_threshold": 95.0, "size_retries": 1}
    try:
        defaults.update(json.loads(config_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    if defaults.get("compression_mode") not in {MODE_LOSSLESS, MODE_ADAPTIVE}:
        defaults["compression_mode"] = MODE_LOSSLESS
    config_path.write_text(json.dumps(defaults, ensure_ascii=False, indent=2), encoding="utf-8")
    return defaults


def configured_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def deduplicate_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique = []
    for path in paths:
        normalized = os.path.normcase(str(path.resolve()))
        if normalized not in seen:
            seen.add(normalized)
            unique.append(path)
    return unique


def mode_selection_needs_warning(previous: str, selected: str) -> bool:
    return selected == MODE_ADAPTIVE and previous != MODE_ADAPTIVE


def map_processing_progress(stage: str, value: float | None, mode: str) -> float | None:
    label = stage.casefold()
    fraction = max(0.0, min(1.0, value if value is not None else 0.0))
    if "подбор качества" in label:
        return 0.42 + 0.18 * fraction
    if "полное crf-кодирование" in label:
        return 0.60 + 0.28 * fraction
    if "без потерь: кодирование h.265" in label:
        return (0.38 if mode == MODE_ADAPTIVE else 0.88) * fraction
    if "проход 1 из 2" in label:
        return 0.43 + 0.22 * fraction
    if "проход 2 из 2" in label:
        return 0.65 + 0.23 * fraction
    if "проверка целостности результата без потерь" in label:
        return 0.42 if mode == MODE_ADAPTIVE else 0.90
    if "полная проверка декодирования результата" in label:
        return 0.90
    if "vmaf" in label:
        return 0.96
    if "финальная проверка и сохранение" in label:
        return 0.99
    return None


def make_report_entry(file_name: str, source_size: int | None, output_size: int | None,
                      status: str, score: float | None = None, output: Path | None = None,
                      message: str | None = None) -> dict:
    saved = source_size - output_size if source_size is not None and output_size is not None else None
    percent = round(saved * 100 / source_size, 2) if saved is not None and source_size else 0.0
    return {"file": file_name, "source_size_bytes": source_size, "output_size_bytes": output_size,
            "saved_bytes": saved, "saved_percent": percent, "status": status, "vmaf": score,
            "output": str(output) if output else None, "message": message}


def bounded_int(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def bounded_float(value, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def publish_file(partial: Path, destination: Path) -> None:
    """Atomically publish without replacing an existing result on Windows."""
    try:
        os.link(partial, destination)
        partial.unlink()
    except (OSError, NotImplementedError):
        # On Windows os.rename is atomic on the same volume and refuses an existing target.
        os.rename(partial, destination)


def file_snapshot(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def wait_until_stable(path: Path, interval: float = 1.0, samples: int = 3) -> tuple[int, int]:
    previous = file_snapshot(path)
    stable = 0
    for _ in range(samples * 3):
        time.sleep(interval)
        current = file_snapshot(path)
        if current == previous:
            stable += 1
            if stable >= samples - 1:
                return current
        else:
            stable = 0
            previous = current
    raise ValueError("Файл ещё копируется или изменяется. Повторите обработку после завершения копирования.")


class AnimatedProgress(tk.Canvas):
    """Percentage bar with a moving highlight clipped to the filled green area."""

    def __init__(self, parent, *, height: int = 16):
        super().__init__(parent, height=height, background="#f1f1f1", borderwidth=0,
                         highlightthickness=1, highlightbackground="#b8b8b8", takefocus=0)
        self.fraction = 0.0
        self.running = False
        self.phase = 0.0
        self.after_id: str | None = None
        self.animation_interval_ms = 40
        self.bind("<Configure>", self._draw)
        self._draw()

    def set_fraction(self, value: float):
        self.fraction = max(0.0, min(1.0, value))
        self._draw()

    def start(self, interval: int = 40):
        if not self.running:
            self.animation_interval_ms = max(16, int(interval))
            self.running = True
            self._animate()

    def stop(self):
        self.running = False
        if self.after_id is not None:
            try:
                self.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None
        self._draw()

    def _animate(self):
        if not self.running:
            return
        self.phase = (self.phase + 0.045) % 1.0
        self._draw()
        self.after_id = self.after(self.animation_interval_ms, self._animate)

    def _draw(self, _event=None):
        self.delete("all")
        width = max(1, self.winfo_width())
        height = max(4, self.winfo_height())
        left, right = 1, max(1, width - 1)
        top, bottom = 1, max(2, height - 1)
        inner_width = max(0, right - left)
        self.create_rectangle(left, top, right, bottom, fill="#e8e8e8", outline="")
        fill_width = int(inner_width * self.fraction)
        if fill_width <= 0:
            return
        fill_right = left + fill_width
        self.create_rectangle(left, top, fill_right, bottom, fill="#2eaa57", outline="")
        if not self.running:
            return
        stripe_width = min(fill_width, max(7, min(46, inner_width // 18)))
        if fill_width <= stripe_width:
            color = "#55c978" if self.phase < 0.5 else "#36b45f"
            self.create_rectangle(left, top, fill_right, bottom, fill=color, outline="")
            return
        travel = fill_width - stripe_width
        position = int((1.0 - abs(2.0 * self.phase - 1.0)) * travel)
        stripe_left = left + position
        first = max(2, stripe_width // 3)
        second = max(2, stripe_width // 3)
        self.create_rectangle(stripe_left, top, min(fill_right, stripe_left + first), bottom,
                              fill="#43bd67", outline="")
        self.create_rectangle(stripe_left + first, top,
                              min(fill_right, stripe_left + first + second), bottom,
                              fill="#7add96", outline="")
        self.create_rectangle(stripe_left + first + second, top, min(fill_right, stripe_left + stripe_width), bottom,
                              fill="#43bd67", outline="")


def fingerprint(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8"))
    with path.open("rb") as stream:
        digest.update(stream.read(min(1024 * 1024, stat.st_size)))
        if stat.st_size > 1024 * 1024:
            stream.seek(max(0, stat.st_size - 1024 * 1024))
            digest.update(stream.read(1024 * 1024))
    return digest.hexdigest()


class History:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            fingerprint TEXT PRIMARY KEY, file_path TEXT NOT NULL, file_size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL, output_path TEXT, output_size INTEGER, codec TEXT,
            status TEXT NOT NULL, processed_at TEXT NOT NULL, app_version TEXT NOT NULL)""")
        self.db.commit()

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def completed(self, key: str, output: Path, *, mode: str = MODE_LOSSLESS,
                  max_size_bytes: int = MAX_SIZE_BYTES) -> bool:
        with self.lock:
            row = self.db.execute("SELECT status, output_size, file_size FROM jobs WHERE fingerprint=?", (key,)).fetchone()
        return bool(row and row[0] in {"SUCCESS", "SUCCESS_SIZE_ONLY", "SUCCESS_LOSSLESS", "SUCCESS_LOSSY_FALLBACK"}
                    and output.exists() and output.stat().st_size == row[1]
                    and output_size_is_valid(mode, int(row[2]), output.stat().st_size, max_size_bytes))

    def record(self, key: str, source: Path, output: Path | None, status: str, codec: str) -> None:
        try:
            stat = source.stat()
            file_size, mtime_ns = stat.st_size, stat.st_mtime_ns
        except OSError:
            file_size, mtime_ns = 0, 0
        with self.lock:
            self.db.execute("""INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (key, str(source), file_size, mtime_ns, str(output) if output else None,
                             output.stat().st_size if output and output.exists() else None, codec, status,
                             datetime.now().astimezone().isoformat(), VERSION))
            self.db.commit()


class MainWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("VideoShrink")
        self.root.geometry("980x650")
        self.root.minsize(840, 540)
        self.base = data_root()
        self.folders = ensure_layout(self.base)
        self.config = load_config(self.base)
        self.config["max_size_bytes"] = bounded_int(self.config.get("max_size_bytes"), MAX_SIZE_BYTES, 1_000_000, MAX_SIZE_BYTES)
        self.config["target_size_bytes"] = bounded_int(
            self.config.get("target_size_bytes"), TARGET_SIZE_BYTES, 1_000_000, self.config["max_size_bytes"] - 1)
        if self.config["target_size_bytes"] >= self.config["max_size_bytes"]:
            self.config["target_size_bytes"] = min(TARGET_SIZE_BYTES, self.config["max_size_bytes"] - 1)
        self.config["quality_threshold"] = bounded_float(self.config.get("quality_threshold"), 95.0, 0.0, 100.0)
        self.config["size_retries"] = bounded_int(self.config.get("size_retries"), 1, 0, 3)
        self.folders["Input"] = configured_path(self.config["input_dir"], self.base)
        self.folders["Output"] = configured_path(self.config["output_dir"], self.base)
        self.folders["Temp"] = configured_path(self.config.get("temp_dir", "Temp"), self.base)
        for name in ("Input", "Output", "Temp"):
            self.folders[name].mkdir(parents=True, exist_ok=True)
        self.history = History(self.base / "history.sqlite3")
        self.messages: queue.Queue = queue.Queue()
        self.stop_requested = threading.Event()
        self.worker: threading.Thread | None = None
        self.processor: VideoProcessor | None = None
        self.processing = False
        self.summary: list[dict] = []
        self.active_stage: str | None = None
        self.active_stage_started_at = 0.0
        self.active_stage_progress: float | None = None
        self.active_stage_total_seconds: float | None = None
        self.last_encode_seconds: float | None = None
        self.active_file_started_at: float | None = None
        self.file_progress = 0.0
        self.fallback_started = False
        logs = logging.getLogger("videoshrink")
        logs.setLevel(logging.INFO)
        handler = RotatingFileHandler(self.folders["Logs"] / "app.log", maxBytes=3_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logs.addHandler(handler)
        self.log = logs
        self._build()
        self.root.after(100, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.refresh()

    def _build(self):
        main = ttk.Frame(self.root, padding=14)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="VideoShrink", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        self.summary_label = ttk.Label(main, text="Готово к поиску видео")
        self.summary_label.pack(anchor="w", pady=(2, 10))
        self.mode_var = tk.StringVar(value=self.config["compression_mode"])
        mode_box = ttk.LabelFrame(main, text="Режим сжатия", padding=(8, 5))
        mode_box.pack(fill="x", pady=(0, 8))
        self.lossless_mode_button = ttk.Radiobutton(
            mode_box, text="1. Максимальное сжатие без потери качества",
            variable=self.mode_var, value=MODE_LOSSLESS,
            command=lambda: self._select_mode(MODE_LOSSLESS))
        self.lossless_mode_button.pack(anchor="w")
        self.adaptive_mode_button = ttk.Radiobutton(
            mode_box, text="2. Если нужно — усилить сжатие до лимита 2 ГБ",
            variable=self.mode_var, value=MODE_ADAPTIVE,
            command=lambda: self._select_mode(MODE_ADAPTIVE))
        self.adaptive_mode_button.pack(anchor="w")
        ttk.Label(mode_box, text="Режим 1 сохраняет каждый кадр без потерь, но размер файла может вырасти. В режиме 2 после неудачного lossless-сжатия или при превышении лимита программа подберёт более компактное H.265-кодирование; качество может немного снизиться.",
                  wraplength=900).pack(anchor="w", pady=(3, 0))
        columns = ("name", "source_size", "output_size", "saved", "status")
        self.table = ttk.Treeview(main, columns=columns, show="headings", height=12)
        for col, title, width in (("name", "Видео", 420), ("source_size", "Было", 105),
                                  ("output_size", "Стало", 105), ("saved", "Экономия", 105),
                                  ("status", "Состояние", 185)):
            self.table.heading(col, text=title)
            self.table.column(col, width=width, anchor="w")
        self.table.pack(fill="both", expand=True)
        self.current = ttk.Label(main, text="Выберите режим и нажмите «Начать обработку»")
        self.current.pack(anchor="w", pady=(10, 3))
        self.progress = AnimatedProgress(main)
        self.progress.pack(fill="x")
        self.percent = ttk.Label(main, text="0%")
        self.percent.pack(anchor="e")
        self.eta_label = ttk.Label(main, text="Оценка времени появится после начала обработки", foreground="#555")
        self.eta_label.pack(anchor="w")
        buttons = ttk.Frame(main)
        buttons.pack(fill="x", pady=(10, 0))
        self.start_button = ttk.Button(buttons, text="Начать обработку", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Остановить", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=6)
        ttk.Button(buttons, text="Папка для видео", command=lambda: self.open_folder(self.folders["Input"])).pack(side="left", padx=6)
        ttk.Button(buttons, text="Готовые видео", command=lambda: self.open_folder(self.folders["Output"])).pack(side="left")
        self.settings_button = ttk.Button(buttons, text="Настройки", command=self.settings)
        self.settings_button.pack(side="right")
        self.path_label = ttk.Label(main, text=f"Рабочая папка: {self.base}", foreground="#666")
        self.path_label.pack(anchor="w", pady=(8, 0))

    def _save_config(self):
        config_path = self.base / "config.json"
        pending = config_path.with_suffix(".json.partial")
        pending.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(pending, config_path)

    def _set_active_stage(self, label: str, progress_value: float | None = None):
        now = time.monotonic()
        if label != self.active_stage:
            if (self.active_stage and "кодирование" in self.active_stage.casefold()
                    and self.active_stage_total_seconds):
                self.last_encode_seconds = self.active_stage_total_seconds
            self.active_stage = label
            self.active_stage_started_at = now
            self.active_stage_progress = None
            self.active_stage_total_seconds = None
        if progress_value is not None:
            value = max(0.0, min(1.0, progress_value))
            self.active_stage_progress = value
            elapsed = now - self.active_stage_started_at
            if value >= 0.05 and elapsed >= 1.0:
                estimate = elapsed / value
                if self.active_stage_total_seconds is None:
                    self.active_stage_total_seconds = estimate
                else:
                    self.active_stage_total_seconds = self.active_stage_total_seconds * 0.7 + estimate * 0.3

    def _estimate_remaining_seconds(self) -> float | None:
        if not self.active_stage:
            return None
        label = self.active_stage.casefold()
        elapsed = max(0.0, time.monotonic() - self.active_stage_started_at)
        total = self.active_stage_total_seconds
        progress = self.active_stage_progress
        if total is not None and progress is not None:
            remaining = total * (1.0 - progress)
            if "проход 1 из 2" in label:
                remaining += total * 1.5
            elif "проход 2 из 2" in label:
                remaining += total * 0.5
            elif "кодирование" in label:
                remaining += total * 0.35
            return max(0.0, remaining)
        if "финальная проверка" in label or "сохранение" in label:
            return max(0.0, 20.0 - elapsed)
        if self.last_encode_seconds:
            if "vmaf" in label:
                return max(0.0, self.last_encode_seconds * 0.5 - elapsed)
            if "проверка" in label or "целостности" in label or "декодирования" in label:
                return max(0.0, self.last_encode_seconds * 0.35 - elapsed)
        return None

    @staticmethod
    def _format_eta(seconds: float) -> str:
        minutes = max(1, int((seconds + 59) // 60))
        if minutes < 60:
            return f"около {minutes} мин"
        hours, rest = divmod(minutes, 60)
        return f"около {hours} ч {rest} мин" if rest else f"около {hours} ч"

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        whole_seconds = max(0, int(seconds))
        if whole_seconds < 60:
            return f"{whole_seconds} сек"
        minutes = whole_seconds // 60
        if minutes < 60:
            return f"{minutes} мин"
        hours, rest = divmod(minutes, 60)
        return f"{hours} ч {rest} мин" if rest else f"{hours} ч"

    def _refresh_eta_label(self):
        if not self.processing:
            return
        if self.active_file_started_at is None:
            self.eta_label.configure(text="Оценка появится после начала обработки файла")
            return
        elapsed = max(0.0, time.monotonic() - self.active_file_started_at)
        remaining = self._estimate_remaining_seconds()
        if remaining is None:
            self.eta_label.configure(
                text=f"Прошло {self._format_elapsed(elapsed)} · рассчитываю оставшееся время…")
        else:
            self.eta_label.configure(
                text=(f"Прошло {self._format_elapsed(elapsed)} · всего {self._format_eta(elapsed + remaining)}"
                      f" · осталось {self._format_eta(remaining)}"))

    def _set_file_progress(self, value: float):
        self.file_progress = max(self.file_progress, max(0.0, min(0.99, value)))
        self.progress.set_fraction(self.file_progress)
        self.percent.configure(text=f"{self.file_progress * 100:.0f}%")

    def _update_file_progress(self, stage: str, value: float | None = None):
        mode = self.config.get("compression_mode", MODE_LOSSLESS)
        mapped = map_processing_progress(stage, value, mode)
        if mapped is not None:
            self._set_file_progress(mapped)
        if "усиленное сжатие" in stage.casefold() or "подбор качества" in stage.casefold():
            self.fallback_started = True

    def _warn_adaptive_mode(self, parent=None) -> bool:
        return messagebox.askyesno(
            "Предупреждение о режиме 2",
            "Сначала программа попробует уменьшить видео без потери кадров. Если это не удастся или результат не уложится в лимит, будет применено более сильное H.265-сжатие. В некоторых случаях качество изображения может немного снизиться. Исходный файл останется нетронутым.",
            parent=parent or self.root)

    def _select_mode(self, mode: str):
        previous = self.config.get("compression_mode", MODE_LOSSLESS)
        if self.processing:
            self.mode_var.set(previous)
            return
        if mode_selection_needs_warning(previous, mode) and not self._warn_adaptive_mode():
            self.mode_var.set(previous)
            return
        self.config["compression_mode"] = mode
        self._save_config()

    def open_folder(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def settings(self):
        win = tk.Toplevel(self.root)
        win.title("Настройки")
        win.transient(self.root)
        win.grab_set()
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Папка исходных видео:").grid(row=0, column=0, sticky="w", pady=4)
        input_dir = tk.StringVar(value=str(self.folders["Input"]))
        ttk.Entry(frame, textvariable=input_dir, width=48).grid(row=0, column=1, sticky="ew")
        ttk.Label(frame, text="Папка готовых видео:").grid(row=1, column=0, sticky="w", pady=4)
        output_dir = tk.StringVar(value=str(self.folders["Output"]))
        ttk.Entry(frame, textvariable=output_dir, width=48).grid(row=1, column=1, sticky="ew")
        ttk.Label(frame, text="Максимальный размер (ГБ, строго меньше):").grid(row=2, column=0, sticky="w", pady=4)
        max_size = tk.StringVar(value=f"{self.config['max_size_bytes'] / 1_000_000_000:.3f}")
        ttk.Entry(frame, textvariable=max_size, width=12).grid(row=2, column=1, sticky="w")
        ttk.Label(frame, text="Целевой размер (ГБ):").grid(row=3, column=0, sticky="w", pady=4)
        target_size = tk.StringVar(value=f"{self.config['target_size_bytes'] / 1_000_000_000:.3f}")
        ttk.Entry(frame, textvariable=target_size, width=12).grid(row=3, column=1, sticky="w")
        ttk.Label(frame, text="Минимальный VMAF (0–100):").grid(row=4, column=0, sticky="w", pady=4)
        quality_threshold = tk.StringVar(value=str(self.config["quality_threshold"]))
        ttk.Entry(frame, textvariable=quality_threshold, width=12).grid(row=4, column=1, sticky="w")
        ttk.Label(frame, text="Повтор при превышении лимита (0–3):").grid(row=5, column=0, sticky="w", pady=4)
        size_retries = tk.StringVar(value=str(self.config["size_retries"]))
        ttk.Entry(frame, textvariable=size_retries, width=12).grid(row=5, column=1, sticky="w")
        ttk.Label(frame, text="Режим сжатия:").grid(row=6, column=0, sticky="nw", pady=4)
        settings_mode = tk.StringVar(value=self.config.get("compression_mode", MODE_LOSSLESS))
        previous_settings_mode = [settings_mode.get()]

        def choose_settings_mode(selected: str):
            previous = previous_settings_mode[0]
            if mode_selection_needs_warning(previous, selected) and not self._warn_adaptive_mode(parent=win):
                settings_mode.set(previous)
                return
            previous_settings_mode[0] = selected
            settings_mode.set(selected)

        mode_frame = ttk.Frame(frame)
        mode_frame.grid(row=6, column=1, sticky="w", pady=4)
        ttk.Radiobutton(mode_frame, text="1. Без потери качества", variable=settings_mode,
                        value=MODE_LOSSLESS,
                        command=lambda: choose_settings_mode(MODE_LOSSLESS)).pack(anchor="w")
        ttk.Radiobutton(mode_frame, text="2. Уложить в 2 ГБ; возможно небольшое снижение качества",
                        variable=settings_mode, value=MODE_ADAPTIVE,
                        command=lambda: choose_settings_mode(MODE_ADAPTIVE)).pack(anchor="w")
        def save():
            new_input = configured_path(input_dir.get(), self.base)
            new_output = configured_path(output_dir.get(), self.base)
            if new_input == new_output or new_input in new_output.parents or new_output in new_input.parents:
                messagebox.showerror("Папки пересекаются", "Папки исходных и готовых видео должны быть отдельными и не вложенными друг в друга.", parent=win)
                return
            try:
                max_bytes = int(float(max_size.get().replace(",", ".")) * 1_000_000_000)
                target_bytes = int(float(target_size.get().replace(",", ".")) * 1_000_000_000)
                threshold = float(quality_threshold.get().replace(",", "."))
                retries = int(size_retries.get())
            except ValueError:
                messagebox.showerror("Проверьте настройки", "Введите числа в поля размера, качества и числа повторов.", parent=win)
                return
            if not 1_000_000 <= max_bytes <= MAX_SIZE_BYTES:
                messagebox.showerror("Проверьте размер", "Максимум должен быть от 0,001 до 2,000 ГБ.", parent=win)
                return
            if not 1_000_000 <= target_bytes < max_bytes:
                messagebox.showerror("Проверьте размер", "Целевой размер должен быть больше нуля и меньше максимального.", parent=win)
                return
            if not 0 <= threshold <= 100 or not 0 <= retries <= 3:
                messagebox.showerror("Проверьте настройки", "VMAF должен быть от 0 до 100, число повторов — от 0 до 3.", parent=win)
                return
            try:
                new_input.mkdir(parents=True, exist_ok=True)
                new_output.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("Не удалось создать папку", str(exc), parent=win)
                return
            self.folders["Input"] = new_input
            self.folders["Output"] = new_output
            self.config["input_dir"] = str(new_input)
            self.config["output_dir"] = str(new_output)
            self.config.pop("lossless", None)
            self.config["max_size_bytes"] = max_bytes
            self.config["target_size_bytes"] = target_bytes
            self.config["quality_threshold"] = threshold
            self.config["size_retries"] = retries
            self.config["compression_mode"] = settings_mode.get()
            self.mode_var.set(settings_mode.get())
            self._save_config()
            self.path_label.configure(text=f"Рабочая папка: {self.base}")
            win.destroy()
            self.refresh()
        ttk.Button(frame, text="Сохранить", command=save).grid(row=7, column=1, sticky="e", pady=12)

    def refresh(self):
        self.table.delete(*self.table.get_children())
        files = sorted((p for p in self.folders["Input"].iterdir() if p.is_file() and p.suffix.lower() == ".mp4"), key=lambda p: p.name.lower())
        self.files = files
        self.summary_label.configure(text=f"Найдено видео: {len(files)}   Папка готовых видео: {self.folders['Output']}")
        for path in files:
            self.table.insert("", "end", iid=str(path),
                              values=(path.name, format_bytes(path.stat().st_size), "—", "—", "Ожидает"))

    def start(self):
        if self.processing:
            return
        self.refresh()
        if not self.files:
            messagebox.showinfo("Нет видео", "Добавьте MP4-файлы в папку исходных видео и повторите запуск.", parent=self.root)
            return
        ffmpeg, ffprobe = self._find_ffmpeg()
        if not ffmpeg or not ffprobe:
            messagebox.showerror("FFmpeg не найден", "Положите ffmpeg.exe и ffprobe.exe рядом с VideoShrink.exe или в папку tools.")
            return
        if (self.folders["Input"] == self.folders["Output"]
                or self.folders["Input"] in self.folders["Output"].parents
                or self.folders["Output"] in self.folders["Input"].parents):
            messagebox.showerror("Папки пересекаются", "Папка готовых видео не может находиться внутри папки исходных видео.")
            return
        self.stop_requested.clear()
        self.processing = True
        self.start_button.configure(state="disabled")
        self.settings_button.configure(state="disabled")
        self.lossless_mode_button.configure(state="disabled")
        self.adaptive_mode_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.stop()
        self.progress.set_fraction(0.0)
        self.progress.start(20)
        self.active_stage = None
        self.active_stage_started_at = time.monotonic()
        self.active_stage_progress = None
        self.active_stage_total_seconds = None
        self.last_encode_seconds = None
        self.active_file_started_at = None
        self.file_progress = 0.0
        self.fallback_started = False
        self.percent.configure(text="0%")
        self.eta_label.configure(text="Оценка появится после начала обработки файла")
        self.current.configure(text="Подготовка очереди…")
        unique_files = deduplicate_paths(list(self.files))
        self.worker = threading.Thread(target=self._process_queue, args=(ffmpeg, ffprobe, unique_files), daemon=True)
        self.worker.start()

    def _find_ffmpeg(self):
        bundled_root = Path(getattr(sys, "_MEIPASS", runtime_root()))
        candidates = [runtime_root(), runtime_root() / "tools", runtime_root() / "_internal",
                      runtime_root() / "_internal" / "tools", bundled_root / "tools",
                      Path(__file__).resolve().parent.parent / "tools"]
        for folder in candidates:
            a, b = folder / "ffmpeg.exe", folder / "ffprobe.exe"
            if a.exists() and b.exists():
                return a, b
        return None, None

    def _output_path(self, source: Path, key: str, max_size_bytes: int = MAX_SIZE_BYTES,
                     mode: str = MODE_LOSSLESS) -> Path:
        stem = source.stem
        candidate = self.folders["Output"] / f"{stem}.mp4"
        if candidate.resolve() == source.resolve():
            candidate = self.folders["Output"] / f"{stem}_{key[:8]}.mp4"
        if candidate.exists():
            if self.history.completed(key, candidate, mode=mode, max_size_bytes=max_size_bytes):
                return candidate
            candidate = self.folders["Output"] / f"{stem}_{key[:8]}.mp4"
            if candidate.exists() and not self.history.completed(
                    key, candidate, mode=mode, max_size_bytes=max_size_bytes):
                candidate = self.folders["Output"] / f"{stem}_{key[:12]}_{time.time_ns() % 100000:05d}.mp4"
        return candidate

    def _process_queue(self, ffmpeg: Path, ffprobe: Path, files: list[Path]):
        files = deduplicate_paths(files)
        rows = []
        successes = failures = skipped = 0
        max_size = self.config["max_size_bytes"]
        mode = self.config.get("compression_mode", MODE_LOSSLESS)
        saved_total = 0

        def stage(source: Path, label: str, index: int):
            self.messages.put(("stage", source, label, index, len(files)))

        for index, source in enumerate(files, 1):
            if self.stop_requested.is_set():
                break
            source_size = None
            try:
                source_size = source.stat().st_size
                source_key = fingerprint(source)
                processing_signature = json.dumps(
                    {"mode": mode, "max_size": max_size,
                     "target_size": self.config["target_size_bytes"],
                     "quality_threshold": self.config["quality_threshold"],
                     "lossless_preset": LOSSLESS_PRESET,
                     "lossy_preset": LOSSY_PRESET,
                     "algorithm": PROCESSING_ALGORITHM},
                    sort_keys=True, separators=(",", ":"))
                key = hashlib.sha256(f"{source_key}|{processing_signature}".encode("utf-8")).hexdigest()
            except OSError as exc:
                failures += 1
                label = "Файл недоступен: " + str(exc)[:120]
                rows.append(make_report_entry(source.name, source_size, None, "FAILED", message=label))
                self.messages.put(("status", source, "Ошибка: " + label, index, len(files)))
                continue
            output = self._output_path(source, key, max_size, mode)
            if self.history.completed(key, output, mode=mode, max_size_bytes=max_size):
                output_size = output.stat().st_size
                rows.append(make_report_entry(source.name, source_size, output_size, "ALREADY_DONE",
                                              output=output, message="Уже готово"))
                successes += 1
                self.messages.put(("status", source, "Уже готово", index, len(files)))
                continue
            partial = output.with_name(output.stem + ".partial.mp4")
            self.messages.put(("status", source, "Анализ", index, len(files)))
            job_started = time.monotonic()
            stage_durations: dict[str, float] = {}
            active_stage_key: str | None = None
            last_stage_key: str | None = None
            active_stage_started = job_started

            def stage_key(label: str) -> str:
                lowered = label.casefold()
                if "подбор качества" in lowered:
                    return "sample_search"
                if "crf-кодирование" in lowered:
                    return "full_crf_encode"
                if "без потерь" in lowered or "lossless" in lowered:
                    return "lossless_encode"
                if "провер" in lowered or "целостност" in lowered or "vmaf" in lowered:
                    return "validation"
                if "ожидание" in lowered:
                    return "wait_for_stable_source"
                if "мест" in lowered:
                    return "disk_space_check"
                if "анализ" in lowered:
                    return "media_probe"
                return "other"

            def record_stage(label: str):
                nonlocal active_stage_key, active_stage_started, last_stage_key
                key_name = stage_key(label)
                last_stage_key = key_name
                if key_name == active_stage_key:
                    return
                now = time.monotonic()
                if active_stage_key is not None:
                    stage_durations[active_stage_key] = stage_durations.get(active_stage_key, 0.0) + (now - active_stage_started)
                active_stage_key = key_name
                active_stage_started = now

            def finish_stage():
                nonlocal active_stage_key
                if active_stage_key is not None:
                    stage_durations[active_stage_key] = stage_durations.get(active_stage_key, 0.0) + (time.monotonic() - active_stage_started)
                    active_stage_key = None

            try:
                stage(source, "Ожидание, пока файл перестанет копироваться", index)
                record_stage("Ожидание, пока файл перестанет копироваться")
                initial_snapshot = wait_until_stable(source)
                source_size = initial_snapshot[0]
                stage(source, "Проверка свободного места", index)
                record_stage("Проверка свободного места")
                needed = (source_size if mode == MODE_LOSSLESS else min(source_size, max_size)) + 150_000_000
                if shutil.disk_usage(self.folders["Output"]).free < needed:
                    raise OSError("Недостаточно свободного места для безопасной обработки")
                if shutil.disk_usage(self.folders["Temp"]).free < 100_000_000:
                    raise OSError("Недостаточно свободного места для временных данных кодирования")
                self.processor = VideoProcessor(ffmpeg, ffprobe, self.folders["Temp"], max_size_bytes=max_size)
                stage(source, "Анализ видеопотока и аудиодорожек", index)
                record_stage("Анализ видеопотока и аудиодорожек")
                media = self.processor.probe(source)
                if not partial.parent.exists():
                    partial.parent.mkdir(parents=True, exist_ok=True)
                def progress(stage: str, value: float):
                    record_stage(stage)
                    lower = stage.casefold()
                    is_encoding = ("кодирование: проход" in lower
                                   or "без потерь: кодирование h.265" in lower)
                    if value <= 0.0 and not is_encoding:
                        self.messages.put(("stage", source, stage, index, len(files)))
                    else:
                        self.messages.put(("progress", source, stage, index, len(files), value))
                status, score = self.processor.encode(
                    media, partial, quality=self.config.get("quality", "compatibility"), progress=progress,
                    target_size=self.config["target_size_bytes"], max_retries=self.config["size_retries"],
                    quality_threshold=self.config["quality_threshold"], mode=mode)
                finish_stage()
                if status == "NO_REDUCTION":
                    skipped += 1
                    label = "Размер не уменьшится; остановлено"
                    self.history.record(key, source, None, status, self.config.get("quality", "compatibility"))
                    rows.append(make_report_entry(source.name, source_size, None, status, message=label))
                    self.messages.put(("status", source, label, index, len(files)))
                    continue
                stage(source, "Финальная проверка и сохранение", index)
                if file_snapshot(source) != initial_snapshot:
                    raise ValueError("Исходный файл изменился во время обработки; результат отброшен")
                output_size = partial.stat().st_size
                if not output_size_is_valid(mode, source_size, output_size, max_size):
                    partial.unlink(missing_ok=True)
                    raise ValueError("Результат нарушает выбранное правило размера; файл не сохранён")
                output.parent.mkdir(parents=True, exist_ok=True)
                publish_file(partial, output)
                self.history.record(key, source, output, status, self.config.get("quality", "compatibility"))
                successes += 1
                if status == "SUCCESS_LOSSLESS":
                    shown = "Готово без потерь"
                elif status == "SUCCESS_LOSSY_FALLBACK":
                    if score is not None and score < self.config["quality_threshold"]:
                        shown = (f"Готово; качество ниже целевого порога "
                                 f"(VMAF {score:.1f} из {self.config['quality_threshold']:.1f})")
                    else:
                        shown = f"Готово с усиленным сжатием (VMAF {score:.1f})"
                else:
                    shown = "Успех" if status == "SUCCESS" else "Готово (размер проверен)"
                output_size = output.stat().st_size
                entry = make_report_entry(source.name, source_size, output_size, status, score, output, shown)
                rows.append(entry)
                self.messages.put(("status", source, shown, index, len(files)))
                self.log.info(
                    "completed file=%s mode=%s status=%s source_bytes=%s output_bytes=%s vmaf=%s "
                    "target_bytes=%s quality_threshold=%s lossless_preset=%s lossy_preset=%s elapsed_seconds=%.2f "
                    "stage_seconds=%s diagnostics=%s",
                    source.name, mode, status, source_size, output_size, score,
                    self.config["target_size_bytes"], self.config["quality_threshold"],
                    LOSSLESS_PRESET, LOSSY_PRESET,
                    time.monotonic() - job_started,
                    json.dumps(stage_durations, sort_keys=True),
                    json.dumps(self.processor.diagnostics if self.processor else {}, sort_keys=True))
            except Cancelled:
                finish_stage()
                partial.unlink(missing_ok=True)
                self.history.record(key, source, None, "CANCELLED", self.config.get("quality", "compatibility"))
                rows.append(make_report_entry(source.name, source_size, None, "CANCELLED", message="Остановлено"))
                self.messages.put(("status", source, "Остановлено", index, len(files)))
                break
            except Exception as exc:
                finish_stage()
                partial.unlink(missing_ok=True)
                failures += 1
                label = str(exc).splitlines()[0][:160]
                self.history.record(key, source, None, "FAILED", self.config.get("quality", "compatibility"))
                rows.append(make_report_entry(source.name, source_size, None, "FAILED", message=label))
                self.messages.put(("status", source, "Ошибка: " + label, index, len(files)))
                self.log.exception(
                    "failed file=%s mode=%s stage=%s elapsed_seconds=%.2f stage_seconds=%s diagnostics=%s",
                    source.name, mode, last_stage_key or "unknown", time.monotonic() - job_started,
                    json.dumps(stage_durations, sort_keys=True),
                    json.dumps(self.processor.diagnostics if self.processor else {}, sort_keys=True))
            finally:
                self.processor = None
        saved_total = sum((entry["saved_bytes"] or 0) for entry in rows
                          if entry["status"] in {"SUCCESS", "SUCCESS_SIZE_ONLY", "SUCCESS_LOSSLESS",
                                                 "SUCCESS_LOSSY_FALLBACK", "ALREADY_DONE"})
        report = {"created_at": datetime.now().astimezone().isoformat(), "found": len(files), "successful": successes,
                  "failed": failures, "skipped": skipped, "compression_mode": mode,
                  "saved_total_bytes": saved_total, "results": rows}
        report_path = self.folders["Logs"] / "last_report.json"
        pending_report = report_path.with_suffix(".json.partial")
        pending_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(pending_report, report_path)
        self.messages.put(("done", successes, failures, skipped, len(files), rows, saved_total))

    def stop(self):
        self.stop_requested.set()
        if self.processor:
            self.processor.stop()
        self.current.configure(text="Останавливаю текущую обработку…")
        self.stop_button.configure(state="disabled")

    def _poll(self):
        try:
            while True:
                item = self.messages.get_nowait()
                if item[0] == "status":
                    _, source, status, index, total = item
                    if str(source) in self.table.get_children(""):
                        self.table.set(str(source), "status", status)
                    self.current.configure(text=f"Файл {index} из {total}: {source.name} — {status}")
                    if status in {"Уже готово", "Готово без потерь", "Успех", "Готово (размер проверен)",
                                  "Размер не уменьшится; остановлено"} or status.startswith("Готово с усиленным") or status.startswith("Ошибка:"):
                        self.file_progress = 1.0
                        self.progress.set_fraction(1.0)
                        self.percent.configure(text="100%")
                    elif status == "Остановлено":
                        pass
                    else:
                        self.file_progress = 0.0
                        self.fallback_started = False
                        self.progress.set_fraction(0.0)
                        self.percent.configure(text="0%")
                        self.active_file_started_at = time.monotonic()
                        self.active_stage = None
                        self.active_stage_started_at = time.monotonic()
                        self.active_stage_progress = None
                        self.active_stage_total_seconds = None
                        self.last_encode_seconds = None
                        self._refresh_eta_label()
                elif item[0] == "progress":
                    _, source, stage, index, total, value = item
                    self.current.configure(text=f"Файл {index} из {total}: {source.name} — {stage}")
                    self._set_active_stage(stage, value)
                    self._update_file_progress(stage, value)
                    self._refresh_eta_label()
                elif item[0] == "stage":
                    _, source, stage, index, total = item
                    self.current.configure(text=f"Файл {index} из {total}: {source.name} — {stage}")
                    self._set_active_stage(stage)
                    self._update_file_progress(stage)
                    self._refresh_eta_label()
                elif item[0] == "done":
                    _, successes, failures, skipped, total, rows, saved_total = item
                    self.processing = False
                    self.processor = None
                    self.progress.stop()
                    if total > 0 and successes + failures + skipped >= total:
                        self.file_progress = 1.0
                        self.progress.set_fraction(1.0)
                        self.percent.configure(text="100%")
                    self.active_stage = None
                    self.active_file_started_at = None
                    self.eta_label.configure(text="Обработка завершена" if not self.stop_requested.is_set()
                                             else "Обработка остановлена")
                    self.start_button.configure(state="normal")
                    self.settings_button.configure(state="normal")
                    self.lossless_mode_button.configure(state="normal")
                    self.adaptive_mode_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.refresh()
                    for entry in rows:
                        source_path = self.folders["Input"] / entry["file"]
                        iid = str(source_path)
                        if not self.table.exists(iid):
                            continue
                        source_size = entry["source_size_bytes"]
                        output_size = entry["output_size_bytes"]
                        saved = entry["saved_bytes"]
                        displayed_status = entry["message"] or entry["status"]
                        self.table.set(iid, "source_size", format_bytes(source_size) if source_size is not None else "—")
                        self.table.set(iid, "output_size", format_bytes(output_size) if output_size is not None else "—")
                        if saved is None:
                            saving_text = "—"
                        elif saved < 0:
                            saving_text = "−" + format_bytes(-saved)
                        else:
                            saving_text = format_bytes(saved)
                        self.table.set(iid, "saved", saving_text)
                        self.table.set(iid, "status", displayed_status)
                    saving_summary = ("Сэкономлено: " + format_bytes(saved_total) if saved_total >= 0
                                      else "Размер вырос на " + format_bytes(-saved_total))
                    self.summary_label.configure(
                        text=f"Завершено. Готово: {successes}; без уменьшения: {skipped}; ошибок: {failures}; файлов: {total}. {saving_summary}")
                    self.current.configure(text="Отчёт сохранён в Logs/last_report.json")
        except queue.Empty:
            pass
        self._refresh_eta_label()
        self.root.after(100, self._poll)

    def _close(self):
        if self.processing:
            if not messagebox.askyesno("Обработка продолжается", "Остановить обработку и закрыть VideoShrink?"):
                return
            self.stop()
            def wait_for_stop():
                if self.processing:
                    self.root.after(150, wait_for_stop)
                else:
                    self.history.close()
                    self.root.destroy()
            self.root.after(150, wait_for_stop)
            return
        self.history.close()
        self.root.destroy()


def main():
    if "--open-input" in sys.argv:
        root = data_root()
        folders = ensure_layout(root)
        os.startfile(folders["Input"])
        return
    root_path = data_root()
    identity = hashlib.sha256(str(root_path.resolve()).casefold().encode("utf-8")).hexdigest()[:24]
    mutex = None
    if os.name == "nt":
        import ctypes
        mutex = ctypes.windll.kernel32.CreateMutexW(None, True, f"Local\\VideoShrink-{identity}")
        if ctypes.windll.kernel32.GetLastError() == 183:
            ctypes.windll.user32.MessageBoxW(None, "VideoShrink уже запущен для этой папки.", "VideoShrink", 0x40)
            return
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.0)
    except tk.TclError:
        pass
    MainWindow(root)
    root.mainloop()
    if mutex:
        ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    main()
