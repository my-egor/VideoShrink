import unittest
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, Mock

from videoshrink import app as app_module
from videoshrink.core import (MAX_SIZE_BYTES, TARGET_SIZE_BYTES, MediaInfo, VideoProcessor,
                              LOSSLESS_PRESET, LOSSY_PRESET, OutputLimitReached,
                              adaptive_target_size, calculate_video_bitrate,
                              classify_media, format_bytes, output_size_is_valid)
from videoshrink.quality_search import QualityCandidate, QualitySearchResult


class FixedSearch:
    def find_candidate(self, source, target_size_bytes, quality_threshold, progress=None):
        return QualitySearchResult(QualityCandidate(24.0, 96.0, target_size_bytes, 10.0), (), True)


class CoreTests(unittest.TestCase):
    def test_default_presets_balance_lossless_size_and_adaptive_speed(self):
        processor = VideoProcessor(Path("ffmpeg.exe"), Path("ffprobe.exe"), Path("temp"))
        self.assertEqual(processor.lossless_preset, LOSSLESS_PRESET)
        self.assertEqual(processor.lossy_preset, LOSSY_PRESET)
        self.assertEqual(processor.quality_search.preset, LOSSY_PRESET)

    def test_bitrate_reserves_audio_and_container_overhead(self):
        result = calculate_video_bitrate(7200, 128_000, 1_900_000_000)
        self.assertGreater(result, 1_800_000)
        self.assertLess(result, 1_900_000)

    def test_rejects_impossible_bitrate_budget(self):
        with self.assertRaises(ValueError):
            calculate_video_bitrate(60, 300_000_000, 1_900_000_000)

    def test_multi_track_audio_budget_is_reserved(self):
        one_track = calculate_video_bitrate(7200, 128_000)
        two_tracks = calculate_video_bitrate(7200, 256_000)
        self.assertEqual(one_track - two_tracks, 128_000)

    def test_classifies_high_bit_depth_as_unsupported(self):
        media = {"streams": [{"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le"}], "format": {"duration": "120", "size": "3000000000"}}
        self.assertEqual(classify_media(media), "UNSUPPORTED")

    def test_classifies_non_420_chroma_as_unsupported(self):
        media = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "pix_fmt": "yuv422p"}], "format": {"duration": "120", "size": "3000000000"}}
        self.assertEqual(classify_media(media), "UNSUPPORTED")

    def test_rejects_additional_data_tracks_instead_of_dropping_them(self):
        media = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "pix_fmt": "yuv420p"}, {"codec_type": "data", "codec_name": "bin_data"}], "format": {"duration": "10", "size": "5000000000"}}
        self.assertEqual(classify_media(media), "UNSUPPORTED")

    def test_size_format_is_human_readable(self):
        self.assertIn("МБ", format_bytes(1_000_000))

    def test_retries_once_with_a_smaller_target_after_size_overflow(self):
        class RetryProcessor(VideoProcessor):
            def __init__(self, temp_dir):
                super().__init__(Path("ffmpeg.exe"), Path("ffprobe.exe"), temp_dir)
                self.targets = []

            def _encode_once(self, source, destination, *, quality, progress, target_size, quality_threshold):
                self.targets.append(target_size)
                if len(self.targets) == 1:
                    destination.write_bytes(b"oversized")
                    raise ValueError("SIZE_LIMIT_EXCEEDED")
                destination.write_bytes(b"accepted")
                return "SUCCESS", 96.0

        with TemporaryDirectory() as temp:
            media = MediaInfo(Path("input.mp4"), MAX_SIZE_BYTES, 60, {}, [], {})
            destination = Path(temp) / "result.partial.mp4"
            processor = RetryProcessor(Path(temp))
            self.assertIn("max_retries", inspect.signature(VideoProcessor.encode).parameters,
                          "encode does not expose its bounded retry count")
            result = processor.encode(media, destination, max_retries=1)
            self.assertEqual(result, ("SUCCESS", 96.0))
            self.assertEqual(processor.targets, [TARGET_SIZE_BYTES, int(TARGET_SIZE_BYTES * 0.95)])
            self.assertEqual(destination.read_bytes(), b"accepted")

    def test_zero_retries_does_not_retry_after_size_overflow(self):
        class RetryProcessor(VideoProcessor):
            def __init__(self, temp_dir):
                super().__init__(Path("ffmpeg.exe"), Path("ffprobe.exe"), temp_dir)
                self.attempts = 0

            def _encode_once(self, source, destination, *, quality, progress, target_size, quality_threshold):
                self.attempts += 1
                destination.write_bytes(b"oversized")
                raise ValueError("SIZE_LIMIT_EXCEEDED")

        with TemporaryDirectory() as temp:
            processor = RetryProcessor(Path(temp))
            media = MediaInfo(Path("input.mp4"), 1_000_000, 60, {}, [], {})
            with self.assertRaisesRegex(ValueError, "SIZE_LIMIT_EXCEEDED"):
                processor.encode(media, Path(temp) / "output.mp4", max_retries=0)
            self.assertEqual(processor.attempts, 1)

    def test_lossless_stops_when_output_reaches_source_size(self):
        class EarlyStopProcessor(VideoProcessor):
            def encode_lossless(self, source, destination, *, progress=None, stop_when_size_reaches=None):
                self.stop_limit = stop_when_size_reaches
                destination.write_bytes(b"x" * source.size)
                raise OutputLimitReached()

        with TemporaryDirectory() as temp:
            media = MediaInfo(Path("input.mp4"), 1_000_000,
                              60, {"codec_name": "h264", "pix_fmt": "yuv420p"}, [],
                              {"streams": [{"codec_type": "video", "codec_name": "h264",
                                            "pix_fmt": "yuv420p"}]})
            destination = Path(temp) / "output.mp4"
            processor = EarlyStopProcessor(Path("ffmpeg.exe"), Path("ffprobe.exe"), Path(temp))
            status, score = processor.encode(media, destination, mode="lossless")
            self.assertEqual((status, score), ("NO_REDUCTION", None))
            self.assertEqual(processor.stop_limit, media.size)
            self.assertFalse(destination.exists())

    def test_adaptive_falls_back_for_source_below_two_gib(self):
        class EarlyStopProcessor(VideoProcessor):
            def __init__(self, ffmpeg, ffprobe, temp_dir):
                super().__init__(ffmpeg, ffprobe, temp_dir, quality_search=FixedSearch())

            def encode_lossless(self, source, destination, *, progress=None, stop_when_size_reaches=None):
                self.lossless_calls = getattr(self, "lossless_calls", 0) + 1
                raise OutputLimitReached()

            def _encode_crf(self, source, destination, *, crf, target_size, max_retries, progress):
                destination.write_bytes(b"smaller")
                return 96.0

        with TemporaryDirectory() as temp:
            media = MediaInfo(Path("input.mp4"), 1_000_000, 60,
                              {"codec_name": "h264", "pix_fmt": "yuv420p"}, [],
                              {"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"}]})
            processor = EarlyStopProcessor(Path("ffmpeg.exe"), Path("ffprobe.exe"), Path(temp))
            destination = Path(temp) / "output.mp4"
            status, score = processor.encode(media, destination, mode="adaptive")
            self.assertEqual((status, score), ("SUCCESS_LOSSY_FALLBACK", 96.0))
            self.assertEqual(processor.lossless_calls, 1)
            self.assertTrue(destination.exists())

    def test_output_size_policy_has_mode_specific_limit(self):
        self.assertTrue(output_size_is_valid("lossless", 3_000_000_000, 2_500_000_000))
        self.assertFalse(output_size_is_valid("adaptive", 3_000_000_000, 2_000_000_000))
        self.assertTrue(output_size_is_valid("adaptive", 1_000_000_000, 900_000_000))
        self.assertFalse(output_size_is_valid("adaptive", 1_000_000_000, 1_000_000_000))

    def test_adaptive_target_leaves_only_one_percent_reserve_for_small_source(self):
        source_size = 1_000_000_000
        self.assertEqual(adaptive_target_size(source_size, TARGET_SIZE_BYTES), 990_000_000)
        self.assertEqual(adaptive_target_size(source_size, 500_000_000), 500_000_000)

    def test_adaptive_target_keeps_default_limit_reserve_for_large_source(self):
        self.assertEqual(adaptive_target_size(MAX_SIZE_BYTES + 1, MAX_SIZE_BYTES), TARGET_SIZE_BYTES)

    @patch("videoshrink.core.subprocess.run")
    def test_strict_rejects_mismatched_decoded_frame_hashes(self, run):
        run.return_value = Mock(returncode=0, stdout="#stream_index, dts, hash\n0,0,abc\n1,0,xyz\n")
        processor = VideoProcessor(Path("ffmpeg.exe"), Path("ffprobe.exe"), Path("temp"))
        self.assertFalse(processor.frame_hashes_match(Path("source.mp4"), Path("output.mp4")))

    @patch("videoshrink.core.subprocess.run")
    def test_strict_accepts_matching_decoded_frame_hashes(self, run):
        run.return_value = Mock(returncode=0, stdout="#stream_index, dts, hash\n0,0,abc\n1,0,abc\n")
        processor = VideoProcessor(Path("ffmpeg.exe"), Path("ffprobe.exe"), Path("temp"))
        self.assertTrue(processor.frame_hashes_match(Path("source.mp4"), Path("output.mp4")))

    def test_adaptive_uses_sample_selected_crf_and_accepts_threshold_warning(self):
        class FakeSearch:
            def find_candidate(self, source, target_size_bytes, quality_threshold, progress=None):
                self.target = target_size_bytes
                return QualitySearchResult(QualityCandidate(23.5, 93.0, 900_000, 12.0), (), False)

        class AdaptiveProcessor(VideoProcessor):
            def encode_lossless(self, source, destination, *, progress=None, stop_when_size_reaches=None):
                raise OutputLimitReached()

            def _encode_crf(self, source, destination, *, crf, target_size, max_retries, progress):
                self.crf = crf
                destination.write_bytes(b"reduced")
                return 94.0

        with TemporaryDirectory() as temp:
            search = FakeSearch()
            processor = AdaptiveProcessor(Path("ffmpeg.exe"), Path("ffprobe.exe"), Path(temp),
                                          quality_search=search, max_size_bytes=2_000_000)
            media = MediaInfo(Path("input.mp4"), 1_000_000, 60,
                              {"codec_name": "h264", "pix_fmt": "yuv420p"}, [],
                              {"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"}]})
            status, score = processor.encode(media, Path(temp) / "out.mp4", mode="adaptive",
                                             target_size=900_000, quality_threshold=95)
            self.assertEqual((status, score), ("SUCCESS_LOSSY_FALLBACK", 94.0))
            self.assertEqual(processor.crf, 23.5)
            self.assertEqual(search.target, 900_000)
    def test_adaptive_fallback_respects_zero_retry_setting(self):
        class FallbackProcessor(VideoProcessor):
            def __init__(self, ffmpeg, ffprobe, temp_dir):
                super().__init__(ffmpeg, ffprobe, temp_dir, quality_search=FixedSearch())

            def encode_lossless(self, source, destination, *, progress=None, stop_when_size_reaches=None):
                self.stop_limit = stop_when_size_reaches
                raise OutputLimitReached()

            def _encode_crf(self, source, destination, *, crf, target_size, max_retries, progress):
                self.retries = max_retries
                destination.write_bytes(b"accepted")
                return 96.0

        with TemporaryDirectory() as temp:
            media = MediaInfo(Path("input.mp4"), MAX_SIZE_BYTES + 1,
                              60, {"codec_name": "h264", "pix_fmt": "yuv420p"}, [],
                              {"streams": [{"codec_type": "video", "codec_name": "h264",
                                            "pix_fmt": "yuv420p"}]})
            destination = Path(temp) / "output.mp4"
            processor = FallbackProcessor(Path("ffmpeg.exe"), Path("ffprobe.exe"), Path(temp))
            status, score = processor.encode(media, destination, mode="adaptive", max_retries=0)
            self.assertEqual((status, score), ("SUCCESS_LOSSY_FALLBACK", 96.0))
            self.assertEqual(processor.stop_limit, MAX_SIZE_BYTES)
            self.assertEqual(processor.retries, 0)

    def test_report_entry_includes_sizes_and_savings(self):
        builder = getattr(app_module, "make_report_entry", None)
        self.assertIsNotNone(builder, "report builder is missing")
        entry = builder("Клип.mp4", 8_000_000, 6_000_000, "SUCCESS", 96.0)
        self.assertEqual(entry["source_size_bytes"], 8_000_000)
        self.assertEqual(entry["output_size_bytes"], 6_000_000)
        self.assertEqual(entry["saved_bytes"], 2_000_000)
        self.assertEqual(entry["saved_percent"], 25.0)


if __name__ == "__main__":
    unittest.main()


