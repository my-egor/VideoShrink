import tempfile
import unittest
from pathlib import Path

from videoshrink.app import ensure_layout, fingerprint


class PathTests(unittest.TestCase):
    def test_creates_expected_data_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            folders = ensure_layout(Path(tmp) / "Видео")
            self.assertEqual(set(folders), {"Input", "Output", "Logs", "Temp"})
            self.assertTrue(all(p.is_dir() for p in folders.values()))

    def test_fingerprint_handles_unicode_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "Поездка — день 1.mp4"
            file.write_bytes(b"mp4 payload")
            self.assertEqual(len(fingerprint(file)), 64)


if __name__ == "__main__":
    unittest.main()
