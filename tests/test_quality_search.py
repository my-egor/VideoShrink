import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from videoshrink.quality_search import (
    NoCandidateFits,
    QualityCandidate,
    choose_candidate,
    QualitySearch,
)


class QualitySearchTests(unittest.TestCase):
    def test_search_returns_best_fitting_candidate_and_marks_quality_warning(self):
        candidates = (
            QualityCandidate(20, 98.0, 1200, 100),
            QualityCandidate(24, 94.0, 900, 90),
            QualityCandidate(28, 91.0, 700, 80),
        )
        result = choose_candidate(candidates, 1000, 95.0)
        self.assertEqual(result.selected.crf, 24)
        self.assertFalse(result.threshold_met)

    def test_search_prefers_highest_quality_candidate_that_meets_threshold(self):
        candidates = (
            QualityCandidate(20, 98.0, 1200, 100),
            QualityCandidate(22, 96.0, 990, 95),
            QualityCandidate(26, 93.0, 800, 80),
        )
        result = choose_candidate(candidates, 1000, 95.0)
        self.assertEqual(result.selected.crf, 22)
        self.assertTrue(result.threshold_met)

    def test_search_fails_if_no_candidate_fits_size_budget(self):
        with self.assertRaises(NoCandidateFits):
            choose_candidate((QualityCandidate(46, 50.0, 1100, 10),), 1000, 95.0)

    def test_search_measures_sample_vmaf_only_for_selected_size_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            search = QualitySearch(Path("ffmpeg.exe"), Path(temp), iterations=2)
            source = type("Source", (), {"path": Path("source.mp4"), "size": 1000,
                                          "duration": 60.0, "audio": []})()
            scored = []

            def measure(_source, crf, **_kwargs):
                return QualityCandidate(crf, None, int(2000 / (2 ** (crf / 20))), 10)

            def score(_source, candidate, **_kwargs):
                scored.append(candidate.crf)
                return replace(candidate, sample_vmaf=97.0)

            with patch.object(search, "_measure_candidate", side_effect=measure), \
                 patch.object(search, "_score_candidate", side_effect=score):
                result = search.find_candidate(source, 1000, 95.0)

            self.assertEqual(scored, [result.selected.crf])
            self.assertTrue(result.threshold_met)

    def test_interpolated_search_converges_near_size_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            search = QualitySearch(Path("ffmpeg.exe"), Path(temp), iterations=4)
            source = type("Source", (), {"path": Path("source.mp4"), "size": 1000,
                                          "duration": 60.0, "audio": []})()

            def measure(_source, crf, **_kwargs):
                size = int(2000 / (2 ** (crf / 20)))
                return QualityCandidate(crf, None, size, 10)

            def score(_source, candidate, **_kwargs):
                return replace(candidate, sample_vmaf=97.0)

            with patch.object(search, "_measure_candidate", side_effect=measure), \
                 patch.object(search, "_score_candidate", side_effect=score):
                result = search.find_candidate(source, 1000, 95.0)

            self.assertLessEqual(result.selected.estimated_size_bytes, 1000)
            self.assertLess(abs(result.selected.crf - 20.0), 2.0)

    def test_sample_uses_precise_mp4_timestamps_for_vmaf_alignment(self):
        with tempfile.TemporaryDirectory() as temp:
            search = QualitySearch(Path("ffmpeg.exe"), Path(temp))
            source = type("Source", (), {"path": Path("source.mp4"), "size": 1000,
                                          "duration": 60.0, "audio": []})()
            process = Mock()
            process.stdout = io.StringIO("")
            process.wait.return_value = 0
            output = Path(temp) / "sample.mp4"
            with patch("videoshrink.quality_search.subprocess.Popen", return_value=process) as popen:
                search._run_sample(source, output, 5.0, 2.0, 24.0, lambda _value: None)
            args = popen.call_args.args[0]
            self.assertEqual(args[-1], str(output))
            self.assertEqual(args[args.index("-video_track_timescale") + 1], "90000")
            self.assertEqual(args[args.index("-fps_mode") + 1], "passthrough")

    def test_sample_failure_includes_bounded_ffmpeg_diagnostic_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            search = QualitySearch(Path("ffmpeg.exe"), Path(temp))
            source = type("Source", (), {"path": Path("source.mp4"), "size": 1000,
                                          "duration": 60.0, "audio": []})()
            process = Mock()
            process.stdout = io.StringIO("frame=1\nError opening output file\n")
            process.wait.return_value = 2
            with patch("videoshrink.quality_search.subprocess.Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "Error opening output file"):
                    search._run_sample(source, Path(temp) / "sample.mp4", 5.0, 2.0, 24.0,
                                       lambda _value: None)


if __name__ == "__main__":
    unittest.main()
