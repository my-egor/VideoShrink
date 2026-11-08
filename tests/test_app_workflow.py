import logging
import queue
import shutil
import threading
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from videoshrink.app import (MODE_ADAPTIVE, MODE_LOSSLESS, History, MainWindow,
                             deduplicate_paths, load_config,
                             mode_selection_needs_warning, map_processing_progress)
from videoshrink.core import Cancelled, MAX_SIZE_BYTES, TARGET_SIZE_BYTES


class AppWorkflowTests(unittest.TestCase):
    def test_duplicate_windows_paths_are_removed_case_insensitively(self):
        paths = [Path("C:/Videos/A.mp4"), Path("c:/videos/a.mp4"), Path("C:/Videos/B.mp4")]
        self.assertEqual(len(deduplicate_paths(paths)), 2)

    def test_adaptive_warning_only_when_operator_changes_mode(self):
        self.assertTrue(mode_selection_needs_warning("lossless", "adaptive"))
        self.assertFalse(mode_selection_needs_warning("adaptive", "adaptive"))
        self.assertFalse(mode_selection_needs_warning("adaptive", "lossless"))

    def test_search_progress_maps_to_monotonic_overall_percent(self):
        values = [map_processing_progress("Подбор качества по фрагментам", p, "adaptive")
                  for p in (0.0, 0.25, 0.5, 1.0)]
        self.assertEqual(values, sorted(values))
        self.assertGreater(values[-1], values[0])

    def test_adaptive_progress_never_moves_back_across_stages(self):
        stages = [
            ("Без потерь: кодирование H.265", 1.0),
            ("Усиленное сжатие: Подбор качества по фрагментам", 0.0),
            ("Усиленное сжатие: Подбор качества по фрагментам", 1.0),
            ("Усиленное сжатие: Полное CRF-кодирование", 0.0),
            ("Усиленное сжатие: Полное CRF-кодирование", 1.0),
            ("Полная проверка декодирования результата", 0.0),
            ("Измерение полного VMAF", 0.0),
            ("Финальная проверка и сохранение", 0.0),
        ]
        values = [map_processing_progress(stage, progress, "adaptive")
                  for stage, progress in stages]
        self.assertEqual(values, sorted(values))

    def test_missing_or_invalid_config_defaults_to_lossless_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = load_config(root)
            self.assertEqual(config["compression_mode"], "lossless")
            (root / "config.json").write_text('{"compression_mode":"unknown"}', encoding="utf-8")
            config = load_config(root)
            self.assertEqual(config["compression_mode"], "lossless")

    def test_history_rejects_output_not_smaller_than_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            output = root / "out.mp4"
            source.write_bytes(b"12345")
            output.write_bytes(b"123456")
            history = History(root / "history.sqlite3")
            try:
                history.record("key", source, output, "SUCCESS", "h265")
                self.assertFalse(history.completed("key", output, mode="lossless"))
            finally:
                history.close()

    def test_lossless_history_accepts_result_above_two_gib_when_source_is_larger(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            output = root / "out.mp4"
            source.write_bytes(b"12345")
            output.write_bytes(b"1234")
            history = History(root / "history.sqlite3")
            try:
                history.record("key", source, output, "SUCCESS_LOSSLESS", "h265")
                with history.lock:
                    history.db.execute("UPDATE jobs SET file_size=? WHERE fingerprint=?",
                                       (2_100_000_000, "key"))
                    history.db.commit()
                self.assertTrue(history.completed("key", output, mode="lossless"))
            finally:
                history.close()

    def test_start_ignores_repeat_while_processing(self):
        window = MainWindow.__new__(MainWindow)
        window.processing = True
        MainWindow.start(window)

    def _make_queue_window(self, root: Path, source: Path, mode=MODE_LOSSLESS):
        folders = {name: root / name for name in ("Input", "Output", "Temp", "Logs")}
        for folder in folders.values():
            folder.mkdir(parents=True, exist_ok=True)
        window = MainWindow.__new__(MainWindow)
        window.folders = folders
        window.config = {
            "max_size_bytes": MAX_SIZE_BYTES,
            "target_size_bytes": TARGET_SIZE_BYTES,
            "quality_threshold": 95.0,
            "size_retries": 1,
            "compression_mode": mode,
            "quality": "compatibility",
        }
        window.messages = queue.Queue()
        window.stop_requested = threading.Event()
        window.processor = None
        window.history = History(root / "history.sqlite3")
        window.log = logging.Logger("test_videoshrink_app_workflow", logging.CRITICAL)
        return window, folders

    def test_cancel_removes_only_current_partial(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "Input" / "clip.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            window, folders = self._make_queue_window(root, source)
            preserved = folders["Output"] / "keep.mp4"
            preserved.write_bytes(b"keep")

            class CancellingProcessor:
                def __init__(self, *args, **kwargs):
                    self.diagnostics = {}

                def probe(self, path):
                    return object()

                def encode(self, _media, destination, **kwargs):
                    destination.write_bytes(b"partial")
                    raise Cancelled()

            try:
                with patch("videoshrink.app.wait_until_stable", side_effect=lambda path: (path.stat().st_size, path.stat().st_mtime_ns)), \
                     patch("videoshrink.app.shutil.disk_usage", return_value=types.SimpleNamespace(free=1_000_000_000)), \
                     patch("videoshrink.app.VideoProcessor", CancellingProcessor):
                    MainWindow._process_queue(window, Path("ffmpeg.exe"), Path("ffprobe.exe"), [source])
                self.assertFalse((folders["Output"] / "clip.partial.mp4").exists())
                self.assertEqual(preserved.read_bytes(), b"keep")
                self.assertFalse((folders["Output"] / "clip.mp4").exists())
            finally:
                window.history.close()

    def test_disk_space_failure_does_not_publish_partial(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "Input" / "clip.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            window, folders = self._make_queue_window(root, source)
            try:
                with patch("videoshrink.app.wait_until_stable", side_effect=lambda path: (path.stat().st_size, path.stat().st_mtime_ns)), \
                     patch("videoshrink.app.shutil.disk_usage", return_value=types.SimpleNamespace(free=0)):
                    MainWindow._process_queue(window, Path("ffmpeg.exe"), Path("ffprobe.exe"), [source])
                self.assertEqual(list(folders["Output"].glob("*.mp4")), [])
                self.assertEqual(list(folders["Temp"].iterdir()), [])
            finally:
                window.history.close()


if __name__ == "__main__":
    unittest.main()
