"""Private, bounded audio/video. Reuses the proven immutable asset lifecycle."""

import hashlib
import json
import math
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException
from google.api_core.exceptions import GoogleAPICallError, NotFound

from queep.images import Images
from queep.models import MediaInfo

MAX_BYTES = 25 * 1024 * 1024
MAX_SECONDS = 300
FRAME_COUNT = 8
SOCIAL_SECONDS = 15


def run(command, timeout=25):
    try:
        return subprocess.run(command, capture_output=True, check=True, timeout=timeout).stdout
    except (subprocess.SubprocessError, OSError) as error:
        # Decoder diagnostics can contain private filenames/metadata.
        raise HTTPException(
            415, "Não foi possível ler esta mídia. Escolha outro arquivo."
        ) from error


def probe(path, max_seconds=MAX_SECONDS):
    raw = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-max_alloc",
            "67108864",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mov,mp3,wav",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,duration",
            "-of",
            "json",
            str(path),
        ],
        timeout=10,
    )
    try:
        metadata = json.loads(raw)
        streams = metadata["streams"]
        durations = [float(metadata["format"]["duration"])] if metadata.get("format", {}).get("duration") else []
        durations += [float(s["duration"]) for s in streams if s.get("duration")]
        duration = max(durations) if durations else 0
        if max_seconds is not None and (
            not math.isfinite(duration) or not 0 < duration <= max_seconds + 0.05
        ):
            raise HTTPException(413, "Escolha um áudio ou vídeo de até 5 minutos.")
        videos = [s for s in streams if s["codec_type"] == "video"]
        audios = [s for s in streams if s["codec_type"] == "audio"]
        if len(videos) > 1 or len(audios) > 1 or not (videos or audios):
            raise ValueError("streams")
        video = videos[0] if videos else None
        if video and (
            video["codec_name"] != "h264"
            or not 0 < video.get("width", 0) * video.get("height", 0) <= 9_000_000
        ):
            raise HTTPException(
                415, "Escolha um vídeo MP4/H.264 de até 4K. O app prepara vídeos de Fotos."
            )
        if not math.isfinite(duration) or duration <= 0:
            duration = SOCIAL_SECONDS if max_seconds is None else duration
        return duration, video, bool(audios)
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(415, "Arquivo de áudio ou vídeo inválido.") from error


def ffmpeg(path, *output, timeout=40, seek=None):
    return run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-max_alloc",
            "67108864",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mov,mp3,wav",
            *(["-ss", str(seek)] if seek else []),
            "-i",
            str(path),
            *map(str, output),
        ],
        timeout,
    )


@dataclass
class PreparedMedia:
    original: bytes
    thumbnail: bytes | None
    info: MediaInfo


def prepare(data):
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413, "Escolha um áudio ou vídeo de até 25 MB.")
    with tempfile.TemporaryDirectory(prefix="queep-media-") as folder:
        source = Path(folder) / "input"
        source.write_bytes(data)
        duration, video, has_audio = probe(source)
        target = Path(folder) / "media.mp4"
        mapping = ["-map", "0:v:0", "-c:v", "copy"] if video else ["-vn"]
        if has_audio:
            mapping += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "96k", "-ac", "2"]
        ffmpeg(
            source,
            *mapping,
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-sn",
            "-dn",
            "-t",
            MAX_SECONDS,
            "-movflags",
            "+faststart",
            target,
            timeout=60,
        )
        original = target.read_bytes()
        if len(original) > MAX_BYTES:
            raise HTTPException(413, "A mídia preparada excede 25 MB. Escolha um trecho menor.")
        stored_duration, _, _ = probe(target)
        if abs(duration - stored_duration) > 1:
            raise HTTPException(415, "Não foi possível preservar a duração completa da mídia.")
        # Decode the stored stream, not just the header. No URLs/subresources are followed.
        ffmpeg(target, "-map", "0:v:0" if video else "0:a:0", "-t", "1", "-f", "null", "-")
        thumbnail = None
        if video:
            thumb = Path(folder) / "thumbnail.jpg"
            ffmpeg(
                target,
                "-frames:v",
                "1",
                "-vf",
                "scale=560:560:force_original_aspect_ratio=decrease",
                "-q:v",
                "4",
                thumb,
            )
            thumbnail = thumb.read_bytes()
        return PreparedMedia(
            original,
            thumbnail,
            MediaInfo(
                sha256=hashlib.sha256(data).hexdigest(),
                content_type="video/mp4" if video else "audio/mp4",
                duration_seconds=stored_duration,
                byte_count=len(original),
                has_audio=has_audio,
                width=video["width"] if video else None,
                height=video["height"] if video else None,
            ),
        )


@dataclass
class MediaEvidence:
    audio: bytes | None
    frames: list[tuple[float, bytes]]


def evidence(data, info):
    """All speech, at most eight visual samples; bounded independently of client metadata."""
    deadline = time.monotonic() + 40

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise HTTPException(
                415, "Esta mídia excedeu o tempo de leitura. Escolha outro arquivo."
            )
        return seconds

    with tempfile.TemporaryDirectory(prefix="queep-analysis-") as folder:
        source = Path(folder) / "media.mp4"
        source.write_bytes(data)
        duration, video, has_audio = probe(source)
        if abs(duration - info.duration_seconds) > 1:
            raise HTTPException(415, "Duração da mídia inconsistente.")
        audio = None
        if has_audio:
            target = Path(folder) / "audio.mp3"
            ffmpeg(
                source,
                "-vn",
                "-map",
                "0:a:0",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "32k",
                "-t",
                MAX_SECONDS,
                target,
                timeout=remaining(),
            )
            audio = target.read_bytes()
            if len(audio) > 1_500_000:
                raise HTTPException(413, "Áudio excedeu o limite de processamento.")
        frames = []
        if video:
            count = min(FRAME_COUNT, max(1, math.ceil(duration / 2)))
            for index in range(count):
                second = duration * (index + 0.5) / count
                target = Path(folder) / f"frame-{index}.jpg"
                ffmpeg(
                    source,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=768:768:force_original_aspect_ratio=decrease",
                    "-q:v",
                    "4",
                    target,
                    seek=second,
                    timeout=remaining(),
                )
                frames.append((round(second, 2), target.read_bytes()))
        return MediaEvidence(audio=audio, frames=frames)


def social_evidence(data):
    """First 15s of speech and a few frames from a social clip. The file is discarded."""
    if not data:
        return MediaEvidence(None, [])
    with tempfile.TemporaryDirectory(prefix="queep-social-") as folder:
        source = Path(folder) / "clip.mp4"
        source.write_bytes(data)
        try:
            duration, video, has_audio = probe(source, max_seconds=None)
        except HTTPException:
            return MediaEvidence(None, [])
        window = min(SOCIAL_SECONDS, duration) if duration else SOCIAL_SECONDS
        audio = None
        if has_audio:
            target = Path(folder) / "audio.mp3"
            try:
                ffmpeg(
                    source,
                    "-vn",
                    "-map",
                    "0:a:0",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "32k",
                    "-t",
                    SOCIAL_SECONDS,
                    target,
                    timeout=25,
                )
                clip = target.read_bytes()
                if clip and len(clip) <= 1_500_000:
                    audio = clip
            except HTTPException:
                audio = None
        frames = []
        if video:
            for ratio in (0.2, 0.5, 0.8):
                second = max(0.1, window * ratio)
                target = Path(folder) / f"frame-{ratio}.jpg"
                try:
                    ffmpeg(
                        source,
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=768:768:force_original_aspect_ratio=decrease",
                        "-q:v",
                        "4",
                        target,
                        seek=second,
                        timeout=15,
                    )
                    frames.append((round(second, 2), target.read_bytes()))
                except HTTPException:
                    continue
        return MediaEvidence(audio=audio, frames=frames)


class MediaFiles(Images):
    # The historical image_uploads collection/prefix is the shared asset ledger.
    # Keeping it preserves all existing expiry, account-erasure and late-writer protections.
    prepare = staticmethod(prepare)

    def kind(self, media):
        return "video" if media.info.content_type == "video/mp4" else "audio"

    def fields(self, media):
        return {"media": media.info.model_dump()}

    def objects(self, media):
        result = [("original", media.original, media.info.content_type)]
        if media.thumbnail:
            result.append(("thumbnail", media.thumbnail, "image/jpeg"))
        return result

    def read(self, uid, ref_id, thumbnail=False):
        reference = self.repo.get(uid, ref_id)
        if not reference.media or (thumbnail and reference.kind != "video"):
            raise HTTPException(404, "Mídia não encontrada")
        name = "thumbnail" if thumbnail else "original"
        path = f"{self.prefix(uid, ref_id, reference.media.sha256)}/{name}"
        try:
            data = self.bucket.blob(path).download_as_bytes(timeout=30, retry=None)
        except NotFound as error:
            raise HTTPException(404, "Mídia não encontrada") from error
        except GoogleAPICallError as error:
            raise HTTPException(503, "Não foi possível carregar a mídia") from error
        return data, "image/jpeg" if thumbnail else reference.media.content_type
