from __future__ import annotations

import re
import math
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


ProgressCallback = Callable[[str, float], None]


class MediaSource(Protocol):
    path: Path
    size: int
    duration: float
    audio: list[dict]


@dataclass(frozen=True)
class QualityCandidate:
    crf: float
    sample_vmaf: float | None
    estimated_size_bytes: int
    estimated_seconds: float


@dataclass(frozen=True)
class QualitySearchResult:
    selected: QualityCandidate
    candidates: tuple[QualityCandidate, ...]
    threshold_met: bool


class NoCandidateFits(ValueError):
    pass


class SearchCancelled(Exception):
    pass


def choose_candidate(candidates: tuple[QualityCandidate, ...] | list[QualityCandidate],
                     target_size_bytes: int, quality_threshold: float) -> QualitySearchResult:
    fitting = [candidate for candidate in candidates
               if candidate.estimated_size_bytes <= target_size_bytes]
    if not fitting:
        raise NoCandidateFits("Не найден параметр CRF, который помещается в целевой размер")
    meets_threshold = [candidate for candidate in fitting
                       if candidate.sample_vmaf is not None
                       and candidate.sample_vmaf >= quality_threshold]
    selected = min(meets_threshold or fitting, key=lambda candidate: candidate.crf)
    return QualitySearchResult(selected, tuple(candidates), bool(meets_threshold))


class QualitySearch:
    """Sample-based x265 CRF search following ab-av1 v0.11.7 search behavior."""

    def __init__(self, ffmpeg: Path, temp_dir: Path, *, preset: str = "medium",
                 sample_duration: float = 1.5, iterations: int = 4,
                 cancel_event: threading.Event | None = None):
        self.ffmpeg = Path(ffmpeg)
        self.temp_dir = Path(temp_dir)
        self.preset = preset
        self.sample_duration = max(0.5, sample_duration)
        self.iterations = max(1, min(iterations, 8))
        self.cancel_event = cancel_event or threading.Event()
        self._active_lock = threading.Lock()
        self._active: subprocess.Popen | None = None

    def stop(self) -> None:
        self.cancel_event.set()
        with self._active_lock:
            process = self._active
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _set_active(self, process: subprocess.Popen | None) -> None:
        with self._active_lock:
            self._active = process

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()

    def find_candidate(self, source: MediaSource, target_size_bytes: int, quality_threshold: float,
                       progress: ProgressCallback | None = None) -> QualitySearchResult:
        if source.duration <= 0 or target_size_bytes <= 0:
            raise ValueError("Не удалось определить длительность или целевой размер")
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        candidates: dict[float, QualityCandidate] = {}
        progress_steps = self.iterations + 3

        def measure(crf: float) -> QualityCandidate:
            key = round(crf, 2)
            if key not in candidates:
                candidates[key] = self._measure_candidate(
                    source, key, progress=progress,
                    progress_start=(len(candidates) / progress_steps),
                    progress_span=(1 / progress_steps))
            return candidates[key]

        low, high = 10.0, 46.0
        best_quality = measure(low)
        if best_quality.estimated_size_bytes > target_size_bytes:
            lowest_quality = measure(high)
            if lowest_quality.estimated_size_bytes > target_size_bytes:
                raise NoCandidateFits("Даже максимальный CRF не укладывается в целевой размер")

            low_size = best_quality.estimated_size_bytes
            high_size = lowest_quality.estimated_size_bytes

            for _ in range(self.iterations):
                if self.cancel_event.is_set():
                    raise SearchCancelled()
                denominator = (math.log(high_size) - math.log(low_size)
                               if high_size > 0 and low_size > 0 else 0.0)
                if denominator >= 0:
                    fraction = 0.5
                else:
                    fraction = (math.log(target_size_bytes) - math.log(low_size)) / denominator
                fraction = max(0.15, min(0.85, fraction))
                crf = round(low + (high - low) * fraction, 1)
                if crf <= low or crf >= high:
                    break
                candidate = measure(crf)
                if candidate.estimated_size_bytes <= target_size_bytes:
                    high = crf
                    high_size = candidate.estimated_size_bytes
                else:
                    low = crf
                    low_size = candidate.estimated_size_bytes
        fitting = [candidate for candidate in candidates.values()
                   if candidate.estimated_size_bytes <= target_size_bytes]
        if not fitting:
            raise NoCandidateFits("Не найден параметр CRF, который помещается в целевой размер")
        selected = min(fitting, key=lambda candidate: candidate.crf)
        scored = self._score_candidate(
            source, selected, progress=progress,
            progress_start=(len(candidates) / progress_steps),
            progress_span=(1 / progress_steps))
        candidates[selected.crf] = scored
        if progress:
            progress("Подбор качества завершён", 1.0)
        return choose_candidate(tuple(candidates.values()), target_size_bytes, quality_threshold)

    def _measure_candidate(self, source: MediaSource, crf: float, *,
                           progress: ProgressCallback | None,
                           progress_start: float, progress_span: float) -> QualityCandidate:
        duration = min(self.sample_duration, source.duration / 3)
        duration = max(0.25, duration)
        starts = []
        for point in (0.1, 0.5, 0.9):
            start = max(0.0, min(source.duration - duration, source.duration * point - duration / 2))
            if all(abs(start - old) > 0.05 for old in starts):
                starts.append(start)
        paths: list[Path] = []
        sizes: list[int] = []
        encode_seconds = 0.0
        try:
            for index, start in enumerate(starts, 1):
                if self.cancel_event.is_set():
                    raise SearchCancelled()
                output = self.temp_dir / f"quality-{threading.get_ident()}-{time.time_ns()}-{index}.mp4"
                paths.append(output)
                label = f"Подбор качества: CRF {crf:g}, фрагмент {index} из {len(starts)}"

                def update(value: float, i=index):
                    if progress:
                        progress(label, progress_start + progress_span * ((i - 1 + value) / len(starts)))

                start_time = time.perf_counter()
                self._run_sample(source, output, start, duration, crf, update)
                encode_seconds += time.perf_counter() - start_time
                sizes.append(output.stat().st_size)

            video_bytes_per_second = sum(size / duration for size in sizes) / len(sizes)
            audio_bitrate = 0
            for stream in source.audio:
                try:
                    audio_bitrate += int(stream.get("bit_rate", 0))
                except (TypeError, ValueError):
                    audio_bitrate += 256_000
            if source.audio and audio_bitrate <= 0:
                audio_bitrate = len(source.audio) * 256_000
            predicted = int(video_bytes_per_second * source.duration * 1.05
                            + audio_bitrate * source.duration / 8 + 1_000_000)
            predicted_seconds = encode_seconds * source.duration / (duration * len(starts))
            return QualityCandidate(crf, None, predicted, predicted_seconds)
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

    def _score_candidate(self, source: MediaSource, candidate: QualityCandidate, *,
                         progress: ProgressCallback | None, progress_start: float,
                         progress_span: float) -> QualityCandidate:
        duration = min(self.sample_duration, source.duration / 3)
        duration = max(0.25, duration)
        starts = []
        for point in (0.1, 0.5, 0.9):
            start = max(0.0, min(source.duration - duration, source.duration * point - duration / 2))
            if all(abs(start - old) > 0.05 for old in starts):
                starts.append(start)
        scores: list[float] = []
        paths: list[Path] = []
        try:
            for index, start in enumerate(starts, 1):
                if self.cancel_event.is_set():
                    raise SearchCancelled()
                output = self.temp_dir / f"quality-score-{threading.get_ident()}-{time.time_ns()}-{index}.mp4"
                paths.append(output)
                label = f"Подбор качества: VMAF фрагмента {index} из {len(starts)}"
                local_progress = 0.0

                def update(value: float, i=index):
                    nonlocal local_progress
                    local_progress = min(1.0, max(local_progress, value))
                    if progress:
                        fraction = (i - 1 + local_progress * 0.5) / len(starts)
                        progress(label, progress_start + progress_span * fraction)

                self._run_sample(source, output, start, duration, candidate.crf, update)
                scores.append(self._sample_vmaf(source, output, start, duration))
                if progress:
                    fraction = index / len(starts)
                    progress(label, progress_start + progress_span * fraction)
            return QualityCandidate(candidate.crf, sum(scores) / len(scores),
                                    candidate.estimated_size_bytes, candidate.estimated_seconds)
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

    def _run_sample(self, source: MediaSource, output: Path, start: float, duration: float,
                    crf: float, progress: Callable[[float], None]) -> None:
        args = [str(self.ffmpeg), "-hide_banner", "-nostdin", "-y", "-ss", f"{start:.3f}",
                "-i", str(source.path), "-t", f"{duration:.3f}", "-map", "0:v:0", "-an",
                "-c:v", "libx265", "-preset", self.preset, "-crf", f"{crf:g}",
                "-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-fps_mode", "passthrough",
                "-video_track_timescale", "90000", "-progress", "pipe:1", "-nostats", str(output)]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._set_active(proc)
        assert proc.stdout is not None
        diagnostic_tail: list[str] = []
        error_lines: list[str] = []
        try:
            for line in proc.stdout:
                stripped = line.strip()
                if stripped and not stripped.startswith(("frame=", "fps=", "stream_", "bitrate=",
                                                         "total_size=", "out_time_", "dup_frames=",
                                                         "drop_frames=", "speed=", "progress=")):
                    diagnostic_tail.append(stripped[-400:])
                    diagnostic_tail = diagnostic_tail[-8:]
                    if any(token in stripped.casefold() for token in ("error", "failed", "invalid")):
                        error_lines.append(stripped[-400:])
                        error_lines = error_lines[-3:]
                if self.cancel_event.is_set():
                    self._terminate(proc)
                    raise SearchCancelled()
                if line.startswith("out_time_ms="):
                    try:
                        progress(min(1.0, max(0.0, int(line.split("=", 1)[1]) / (duration * 1_000_000))))
                    except ValueError:
                        pass
            code = proc.wait()
            if self.cancel_event.is_set():
                raise SearchCancelled()
        except Exception:
            self._terminate(proc)
            raise
        finally:
            proc.stdout.close()
            self._set_active(None)
        if code:
            details = " | ".join(error_lines or diagnostic_tail)
            suffix = f": {details}" if details else ""
            raise RuntimeError(f"FFmpeg завершил sample-кодирование с кодом {code}{suffix}")

    def _sample_vmaf(self, source: MediaSource, sample: Path, start: float, duration: float) -> float:
        expr = ("[0:v]setpts=PTS-STARTPTS[distorted];"
                "[1:v]setpts=PTS-STARTPTS[reference];"
                "[distorted][reference]libvmaf")
        args = [str(self.ffmpeg), "-hide_banner", "-loglevel", "info", "-i", str(sample),
                "-ss", f"{start:.3f}", "-i", str(source.path), "-t", f"{duration:.3f}",
                "-filter_complex", expr, "-an", "-f", "null", "NUL"]
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace",
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._set_active(process)
        try:
            while True:
                if self.cancel_event.is_set():
                    self._terminate(process)
                    raise SearchCancelled()
                try:
                    stdout, stderr = process.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except Exception:
            self._terminate(process)
            raise
        finally:
            self._set_active(None)
        if process.returncode:
            raise RuntimeError("Не удалось измерить VMAF фрагмента")
        match = re.search(r"VMAF score:\s*([\d.]+)", stderr + stdout)
        if not match:
            raise RuntimeError("FFmpeg не вернул оценку VMAF для фрагмента")
        return float(match.group(1))
