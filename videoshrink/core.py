from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .quality_search import NoCandidateFits, QualitySearch, SearchCancelled

TARGET_SIZE_BYTES = 1_900_000_000
MAX_SIZE_BYTES = 2_000_000_000
QUALITY_THRESHOLD = 95.0
LOSSLESS_PRESET = "medium"
LOSSY_PRESET = "fast"
PROCESSING_ALGORITHM = "native-sample-crf-v1"


def output_size_is_valid(mode: str, source_size: int, output_size: int,
                         max_size_bytes: int = MAX_SIZE_BYTES) -> bool:
    if source_size <= 0 or output_size <= 0 or output_size >= source_size:
        return False
    if mode not in {"lossless", "adaptive", "lossy"}:
        return False
    if mode == "adaptive":
        limit = min(max_size_bytes, MAX_SIZE_BYTES)
        return source_size < limit or output_size < limit
    return True


def adaptive_target_size(source_size: int, target_size: int,
                         max_size_bytes: int = MAX_SIZE_BYTES) -> int:
    limit = min(max_size_bytes, MAX_SIZE_BYTES)
    if source_size < limit:
        return min(target_size, int(source_size * 0.99), limit - 1)
    return min(target_size, int(limit * 0.95), limit - 1)


def format_bytes(value: int) -> str:
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    size = float(value)
    for unit in units:
        if size < 1000 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} ГБ"


def _fraction(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        left, _, right = value.partition("/")
        return float(left) / float(right or 1)
    except (ValueError, ZeroDivisionError):
        return None


def calculate_video_bitrate(duration: float, audio_bitrate: int, target_size: int = TARGET_SIZE_BYTES) -> int:
    if duration <= 0:
        raise ValueError("Не удалось определить длительность видео")
    # 5% запас под MP4 muxing/metadata, плюс резерв по целевому размеру из ТЗ.
    total = int(target_size * 8 * 0.95 / duration)
    video = total - max(audio_bitrate, 96_000) - 32_000
    if video < 100_000:
        raise ValueError("Недостаточный бюджет битрейта для сохранения звуковых дорожек")
    return video


def classify_media(media: dict) -> str:
    videos = [s for s in media.get("streams", []) if s.get("codec_type") == "video"]
    if len(videos) != 1:
        return "UNSUPPORTED"
    stream = videos[0]
    pix_fmt = stream.get("pix_fmt", "")
    if (any(token in pix_fmt for token in ("10", "12", "14", "16"))
            or stream.get("color_transfer") in {"smpte2084", "arib-std-b67"}
            or stream.get("color_primaries") == "bt2020"):
        return "UNSUPPORTED"
    if pix_fmt != "yuv420p":
        return "UNSUPPORTED"
    if stream.get("codec_name") not in {"h264", "hevc", "mpeg4", "av1", "vp9", "mjpeg"}:
        return "UNSUPPORTED"
    if any(s.get("codec_type") == "data" for s in media.get("streams", [])):
        return "UNSUPPORTED"
    # This profile does not map subtitle, attachment or timed-metadata streams to MP4.
    if any(s.get("codec_type") not in {"video", "audio"} for s in media.get("streams", [])):
        return "UNSUPPORTED"
    return "SUPPORTED"


@dataclass
class MediaInfo:
    path: Path
    size: int
    duration: float
    video: dict
    audio: list[dict]
    raw: dict


class Cancelled(Exception):
    pass


class OutputLimitReached(Exception):
    """The current encoded file has already reached an unusable size."""


class VideoProcessor:
    def __init__(self, ffmpeg: Path, ffprobe: Path, temp_dir: Path, codec: str = "h264", lossless: bool = False,
                 max_size_bytes: int = MAX_SIZE_BYTES, quality_search: QualitySearch | None = None,
                 preset: str | None = None,
                 lossless_preset: str = LOSSLESS_PRESET, lossy_preset: str = LOSSY_PRESET):
        if not 0 < max_size_bytes <= MAX_SIZE_BYTES:
            raise ValueError("Максимальный размер должен быть больше нуля и не превышать 2 000 000 000 байт")
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.temp_dir = temp_dir
        self.codec = codec
        self.lossless = lossless
        self.max_size_bytes = max_size_bytes
        self.lossless_preset = preset or lossless_preset
        self.lossy_preset = preset or lossy_preset
        self.preset = self.lossy_preset
        self.cancel = threading.Event()
        self.active: subprocess.Popen | None = None
        self.diagnostics: dict[str, int | float | None] = {
            "selected_crf": None,
            "sample_estimated_size_bytes": None,
            "sample_vmaf": None,
            "encode_attempts": 0,
            "last_encode_crf": None,
            "last_candidate_size_bytes": None,
        }
        self.quality_search = quality_search or QualitySearch(ffmpeg, temp_dir, preset=self.lossy_preset,
                                                               cancel_event=self.cancel)

    def stop(self) -> None:
        self.cancel.set()
        if self.active and self.active.poll() is None:
            self.active.terminate()
        self.quality_search.stop()

    def _run(self, args: list[str], *, progress: Callable[[float], None] | None = None,
             duration: float | None = None, check: bool = True,
             output_limit: tuple[Path, int] | None = None) -> str:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.active = proc
        lines: list[str] = []

        def terminate_process():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

        try:
            assert proc.stdout
            for line in proc.stdout:
                if self.cancel.is_set():
                    terminate_process()
                    raise Cancelled()
                line = line.rstrip()
                if output_limit and line.startswith("total_size="):
                    output_path, max_bytes = output_limit
                    try:
                        if output_path.stat().st_size >= max_bytes:
                            terminate_process()
                            raise OutputLimitReached()
                    except FileNotFoundError:
                        pass
                if progress and line.startswith("out_time_ms=") and duration:
                    try:
                        ratio = int(line.split("=", 1)[1]) / (duration * 1_000_000)
                        progress(max(0.0, min(1.0, ratio)))
                    except ValueError:
                        pass
                elif line and ("error" in line.lower() or len(lines) < 30):
                    lines.append(line[-500:])
            code = proc.wait()
        finally:
            if proc.stdout:
                proc.stdout.close()
            self.active = None
        if check and code != 0:
            raise RuntimeError("\n".join(lines[-12:]) or f"FFmpeg завершился с кодом {code}")
        return "\n".join(lines)

    def probe(self, path: Path) -> MediaInfo:
        result = subprocess.run([str(self.ffprobe), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                                check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = json.loads(result.stdout)
        videos = [s for s in raw.get("streams", []) if s.get("codec_type") == "video"]
        if not videos:
            raise ValueError("Видеодорожка не найдена")
        return MediaInfo(path, path.stat().st_size, float(raw.get("format", {}).get("duration") or 0),
                         videos[0], [s for s in raw.get("streams", []) if s.get("codec_type") == "audio"], raw)

    def frame_hashes(self, path: Path) -> tuple[str, ...]:
        result = subprocess.run([str(self.ffmpeg), "-v", "error", "-xerror", "-i", str(path),
                                 "-map", "0:v:0", "-f", "framemd5", "-"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise ValueError("Не удалось декодировать кадры для точной проверки")
        return tuple(line.rsplit(",", 1)[-1].strip() for line in result.stdout.splitlines()
                     if line and not line.startswith("#") and "," in line)

    def frame_hashes_match(self, source: Path, output: Path) -> bool:
        result = subprocess.run([str(self.ffmpeg), "-v", "error", "-xerror", "-i", str(source),
                                 "-i", str(output), "-map", "0:v:0", "-map", "1:v:0",
                                 "-map", "0:a?", "-map", "1:a?",
                                 "-f", "framemd5", "-"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            return False
        streams = {0: [], 1: []}
        for line in result.stdout.splitlines():
            if not line or line.startswith("#") or "," not in line:
                continue
            fields = line.split(",")
            try:
                index = int(fields[0].strip())
            except (ValueError, IndexError):
                continue
            if index in streams:
                streams[index].append(fields[-1].strip())
        return bool(streams[0]) and streams[0] == streams[1]

    def validate(self, output: Path, source: MediaInfo, *, check_size_limit: bool = True,
                 verify_frame_hashes: bool = False) -> tuple[bool, str]:
        if not output.exists() or output.stat().st_size == 0:
            return False, "Пустой результат"
        if check_size_limit and output.stat().st_size >= self.max_size_bytes:
            return False, "SIZE_LIMIT_EXCEEDED"
        try:
            if verify_frame_hashes:
                if not self.frame_hashes_match(source.path, output):
                    return False, "Декодированные кадры отличаются от оригинала"
            else:
                result = subprocess.run([str(self.ffmpeg), "-v", "error", "-xerror", "-i", str(output), "-f", "null", "NUL"],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=None)
                if result.returncode:
                    return False, "Результирующее видео не прошло полную проверку декодирования"
            encoded = self.probe(output)
            if abs(encoded.duration - source.duration) > max(2.0, source.duration * 0.01):
                return False, "Продолжительность результата отличается от оригинала"
            if encoded.video.get("width") != source.video.get("width") or encoded.video.get("height") != source.video.get("height"):
                return False, "Изменилось разрешение видео"
            if len(encoded.audio) != len(source.audio):
                return False, "В результате потеряна аудиодорожка"
            return True, "SUCCESS_SIZE_ONLY"
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            return False, str(exc)

    def vmaf(self, source: Path, output: Path) -> float | None:
        expr = ("[0:v]setpts=PTS-STARTPTS[distorted];"
                "[1:v]setpts=PTS-STARTPTS[reference];"
                "[distorted][reference]libvmaf")
        try:
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            run = subprocess.run([str(self.ffmpeg), "-hide_banner", "-loglevel", "info", "-i", str(output), "-i", str(source),
                                  "-filter_complex", expr, "-an", "-f", "null", "NUL"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if run.returncode:
                return None
            import re
            match = re.search(r"VMAF score:\s*([\d.]+)", run.stderr + run.stdout)
            return float(match.group(1)) if match else None
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def encode(self, source: MediaInfo, destination: Path, *, quality: str = "compatibility",
               progress: Callable[[str, float], None] | None = None, target_size: int = TARGET_SIZE_BYTES,
               max_retries: int = 1, quality_threshold: float = QUALITY_THRESHOLD,
               mode: str = "lossy") -> tuple[str, float | None]:
        if self.cancel.is_set():
            raise Cancelled()
        if not 0 <= max_retries <= 3:
            raise ValueError("Число повторов должно быть от 0 до 3")
        if self.lossless and mode == "lossy":
            mode = "lossless"
        if mode in {"lossless", "adaptive"}:
            if classify_media(source.raw) != "SUPPORTED":
                raise ValueError("UNSUPPORTED: для этого видео нужен отдельный профиль (HDR, битность, цветность или дополнительные потоки)")
            if mode == "lossless":
                size_limit = source.size
                try:
                    self.encode_lossless(source, destination, progress=progress,
                                         stop_when_size_reaches=size_limit)
                except OutputLimitReached:
                    destination.unlink(missing_ok=True)
                    if progress:
                        progress("Готовый файл не станет меньше исходника; остановлено", 1.0)
                    return "NO_REDUCTION", None
                if not output_size_is_valid(mode, source.size, destination.stat().st_size, self.max_size_bytes):
                    destination.unlink(missing_ok=True)
                    return "NO_REDUCTION", None
                return "SUCCESS_LOSSLESS", None

            lossless_path = destination.with_name(destination.stem + ".lossless.partial.mp4")
            try:
                size_limit = min(source.size, self.max_size_bytes)
                self.encode_lossless(source, lossless_path, progress=progress,
                                     stop_when_size_reaches=size_limit)
                if output_size_is_valid(mode, source.size, lossless_path.stat().st_size, self.max_size_bytes):
                    os.replace(lossless_path, destination)
                    return "SUCCESS_LOSSLESS", None
                if progress:
                    progress("Результат без потерь превышает лимит; запускаю усиленное сжатие", 0.0)
                lossless_path.unlink(missing_ok=True)
            except OutputLimitReached:
                lossless_path.unlink(missing_ok=True)
                if progress:
                    progress("Lossless не уменьшит файл; запускаю поиск параметров сжатия", 0.0)
            finally:
                lossless_path.unlink(missing_ok=True)

            def fallback_progress(stage: str, value: float):
                if progress:
                    progress("Усиленное сжатие с небольшой потерей качества: " + stage, value)

            effective_limit = min(self.max_size_bytes, MAX_SIZE_BYTES)
            fallback_target = adaptive_target_size(source.size, target_size, effective_limit)
            if progress:
                progress("Подбор качества по фрагментам", 0.0)
            try:
                search_result = self.quality_search.find_candidate(
                    source, fallback_target, quality_threshold,
                    progress=fallback_progress if progress else None)
            except SearchCancelled as exc:
                raise Cancelled() from exc
            except NoCandidateFits as exc:
                raise ValueError(str(exc)) from exc
            self.diagnostics.update({
                "selected_crf": search_result.selected.crf,
                "sample_estimated_size_bytes": search_result.selected.estimated_size_bytes,
                "sample_vmaf": search_result.selected.sample_vmaf,
            })
            try:
                score = self._encode_crf(source, destination, crf=search_result.selected.crf,
                                         target_size=fallback_target, max_retries=max_retries,
                                         progress=fallback_progress if progress else None)
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            if not output_size_is_valid(mode, source.size, destination.stat().st_size, self.max_size_bytes):
                destination.unlink(missing_ok=True)
                raise ValueError("Результат не меньше исходника или превышает ограничение размера")
            if score is None:
                destination.unlink(missing_ok=True)
                raise ValueError("Не удалось проверить качество усиленного сжатия; результат не сохранён")
            return "SUCCESS_LOSSY_FALLBACK", score

        if mode != "lossy":
            raise ValueError("Неизвестный режим сжатия")
        result = self._encode_lossy(source, destination, quality=quality, progress=progress,
                                    target_size=target_size, max_retries=max_retries,
                                    quality_threshold=quality_threshold)
        if destination.exists() and not output_size_is_valid(mode, source.size, destination.stat().st_size,
                                                              self.max_size_bytes):
            destination.unlink(missing_ok=True)
            raise ValueError("Результат не меньше исходника")
        return result

    def encode_lossless(self, source: MediaInfo, destination: Path, *,
                        progress: Callable[[str, float], None] | None = None,
                        stop_when_size_reaches: int | None = None,
                        preset: str | None = None) -> None:
        """Re-encode decoded YUV420P frames losslessly with x265 and copy audio streams."""
        if self.cancel.is_set():
            raise Cancelled()
        if classify_media(source.raw) != "SUPPORTED":
            raise ValueError("UNSUPPORTED: для этого видео нужен отдельный профиль (HDR, битность, цветность или дополнительные потоки)")
        destination.parent.mkdir(parents=True, exist_ok=True)
        common = [str(self.ffmpeg), "-hide_banner", "-nostdin", "-y", "-noautorotate", "-i", str(source.path),
                  "-map", "0:v:0"]
        if source.audio:
            common += ["-map", "0:a?"]
        common += ["-map_metadata", "0", "-map_chapters", "0", "-map_metadata:s:v:0", "0:s:v:0"]
        for audio_index in range(len(source.audio)):
            common += [f"-map_metadata:s:a:{audio_index}", f"0:s:a:{audio_index}"]
        cb = (lambda value: progress("Без потерь: кодирование H.265", value)) if progress else None
        if progress:
            progress("Подготовка кодирования без потерь", 0.0)
        args = common + ["-c:v", "libx265", "-preset", preset or self.lossless_preset, "-x265-params", "lossless=1",
                         "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]
        args += ["-c:a", "copy"] if source.audio else ["-an"]
        args += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(destination)]
        output_limit = ((destination, stop_when_size_reaches)
                        if stop_when_size_reaches is not None else None)
        self._run(args, progress=cb, duration=source.duration, output_limit=output_limit)
        if stop_when_size_reaches is not None and destination.stat().st_size >= stop_when_size_reaches:
            raise OutputLimitReached()
        if progress:
            progress("Проверка целостности результата без потерь", 0.0)
        valid, reason = self.validate(destination, source, check_size_limit=False, verify_frame_hashes=True)
        if not valid:
            raise ValueError(reason)

    def _encode_crf(self, source: MediaInfo, destination: Path, *, crf: float, target_size: int,
                    max_retries: int, progress: Callable[[str, float], None] | None) -> float:
        destination.parent.mkdir(parents=True, exist_ok=True)
        current_crf = crf
        effective_limit = min(self.max_size_bytes, MAX_SIZE_BYTES)
        hard_limit = effective_limit if source.size >= effective_limit else source.size
        common = [str(self.ffmpeg), "-hide_banner", "-nostdin", "-y", "-noautorotate", "-i", str(source.path),
                  "-map", "0:v:0"]
        if source.audio:
            common += ["-map", "0:a?"]
        common += ["-map_metadata", "0", "-map_chapters", "0", "-movflags", "+faststart"]
        for audio_index in range(len(source.audio)):
            common += [f"-map_metadata:s:a:{audio_index}", f"0:s:a:{audio_index}"]
        for attempt in range(max_retries + 1):
            if self.cancel.is_set():
                raise Cancelled()
            stage = f"Полное CRF-кодирование: попытка {attempt + 1} из {max_retries + 1}"
            self.diagnostics["encode_attempts"] = attempt + 1
            self.diagnostics["last_encode_crf"] = current_crf
            if progress:
                progress(stage, 0.0)
            cb = (lambda value, stage=stage: progress(stage, value)) if progress else None
            args = common + ["-c:v", "libx265", "-preset", self.lossy_preset, "-crf", f"{current_crf:g}",
                             "-pix_fmt", "yuv420p"]
            args += ["-c:a", "copy"] if source.audio else ["-an"]
            args += ["-progress", "pipe:1", "-nostats", str(destination)]
            self._run(args, progress=cb, duration=source.duration)
            if progress:
                progress("Полная проверка декодирования результата", 0.0)
            valid, reason = self.validate(destination, source, check_size_limit=False)
            if not valid:
                destination.unlink(missing_ok=True)
                raise ValueError(reason)
            if progress:
                progress("Измерение полного VMAF", 0.0)
            score = self.vmaf(source.path, destination)
            if score is None:
                destination.unlink(missing_ok=True)
                raise ValueError("Не удалось проверить качество VMAF")
            size = destination.stat().st_size
            self.diagnostics["last_candidate_size_bytes"] = size
            if size < source.size and (source.size < effective_limit or size < effective_limit):
                return score
            destination.unlink(missing_ok=True)
            if attempt >= max_retries:
                raise ValueError("SIZE_LIMIT_EXCEEDED: не удалось получить файл меньше исходника и лимита")
            ratio = max(1.0001, size / max(1, hard_limit - 1))
            current_crf = min(51.0, current_crf + min(12.0, max(1.0, 6.0 * math.log2(ratio))))
            if current_crf >= 51.0 and attempt + 1 < max_retries + 1:
                current_crf = 51.0
        raise ValueError("Не удалось подобрать подходящий CRF")

    def _encode_lossy(self, source: MediaInfo, destination: Path, *, quality: str,
                      progress: Callable[[str, float], None] | None, target_size: int,
                      max_retries: int, quality_threshold: float) -> tuple[str, float | None]:
        if self.cancel.is_set():
            raise Cancelled()
        if not 0 < target_size < self.max_size_bytes:
            raise ValueError("Целевой размер должен быть больше нуля и меньше максимального")
        destination.parent.mkdir(parents=True, exist_ok=True)
        retries = 0
        current_target = target_size
        while True:
            try:
                return self._encode_once(source, destination, quality=quality, progress=progress,
                                         target_size=current_target, quality_threshold=quality_threshold)
            except ValueError as exc:
                if str(exc) != "SIZE_LIMIT_EXCEEDED" or retries >= max_retries:
                    raise
                retries += 1
                if progress:
                    progress(f"Повтор {retries} из {max_retries}: уменьшаю целевой битрейт", 0.0)
                destination.unlink(missing_ok=True)
                current_target = int(current_target * 0.95)
                if current_target <= 0:
                    raise ValueError("Недостаточный целевой размер для повторной попытки") from exc

    def _encode_once(self, source: MediaInfo, destination: Path, *, quality: str,
                     progress: Callable[[str, float], None] | None, target_size: int,
                     quality_threshold: float) -> tuple[str, float | None]:
        if classify_media(source.raw) != "SUPPORTED":
            raise ValueError("UNSUPPORTED: HDR, цветность кроме 4:2:0 или дополнительные потоки требуют отдельного профиля")
        audio_bitrate = 0
        for stream in source.audio:
            try:
                audio_bitrate += int(stream.get("bit_rate", 0))
            except (TypeError, ValueError):
                audio_bitrate += 256_000
        if source.audio and audio_bitrate <= 0:
            audio_bitrate = len(source.audio) * 256_000
        target_bitrate = calculate_video_bitrate(source.duration, audio_bitrate, target_size)
        codec = "libx265" if quality == "maximum" else "libx264"
        audio_opts = ["-c:a", "copy"] if source.audio else ["-an"]
        passlog = self.temp_dir / f"pass-{os.getpid()}-{time.time_ns()}"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        common = [str(self.ffmpeg), "-hide_banner", "-nostdin", "-y", "-noautorotate", "-i", str(source.path), "-map", "0:v:0"]
        if source.audio:
            common += ["-map", "0:a?"]
        common += ["-map_metadata", "0", "-map_chapters", "0"]
        common += ["-map_metadata:s:v:0", "0:s:v:0"]
        for audio_index in range(len(source.audio)):
            common += [f"-map_metadata:s:a:{audio_index}", f"0:s:a:{audio_index}"]
        preset = "slow"
        try:
            for pass_number in (1, 2):
                stage = f"Кодирование: проход {pass_number} из 2"
                cb = (lambda value, stage=stage: progress(stage, value)) if progress else None
                args = common + ["-c:v", codec, "-preset", preset, "-b:v", str(target_bitrate),
                                 "-pix_fmt", "yuv420p", "-pass", str(pass_number), "-passlogfile", str(passlog),
                                 "-progress", "pipe:1", "-nostats"]
                if pass_number == 1:
                    args += ["-an", "-sn", "-f", "null", "NUL"]
                else:
                    args += audio_opts + ["-movflags", "+faststart", str(destination)]
                self._run(args, progress=cb, duration=source.duration)
            if progress:
                progress("Полная проверка декодирования результата", 0.0)
            valid, reason = self.validate(destination, source)
            if not valid:
                raise ValueError(reason)
            if progress:
                progress("Измерение качества изображения (VMAF)", 0.0)
            score = self.vmaf(source.path, destination)
            if score is not None and score < quality_threshold:
                raise ValueError(f"QUALITY_LIMIT: VMAF {score:.1f} ниже порога {quality_threshold:.0f}")
            return ("SUCCESS" if score is not None else "SUCCESS_SIZE_ONLY"), score
        finally:
            for suffix in (".log", ".log.mbtree"):
                try:
                    Path(str(passlog) + suffix).unlink(missing_ok=True)
                except OSError:
                    pass
