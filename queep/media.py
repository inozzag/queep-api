"""Private, bounded audio/video. Reuses the proven immutable asset lifecycle."""

import hashlib
import json
import math
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException
from google.api_core.exceptions import GoogleAPICallError, NotFound

from queep.images import (
    CARD_EDGE,
    SHARP_EDGE,
    Images,
    _preview_slots,
    preview_edge,
    recall_bytes,
    remember_bytes,
)
from queep.models import MediaInfo

MAX_BYTES = 25 * 1024 * 1024
MAX_SECONDS = 300
FRAME_COUNT = 8
SOCIAL_SECONDS = 15
LOOP_SECONDS = 8
LOOP_EDGE = 960
MAX_LOOP_BYTES = 4 * 1024 * 1024


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
        duration = metadata.get("format", {}).get("duration")
        durations = [float(duration)] if duration else []
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


def poster_from_video(data: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="queep-poster-") as folder:
        source = Path(folder) / "input.mp4"
        thumb = Path(folder) / "thumbnail.jpg"
        source.write_bytes(data)
        ffmpeg(
            source,
            "-frames:v",
            "1",
            "-vf",
            f"scale={CARD_EDGE}:{CARD_EDGE}:force_original_aspect_ratio=decrease",
            "-q:v",
            "3",
            thumb,
            timeout=20,
        )
        return thumb.read_bytes()


def ffmpeg(path, *output, timeout=40, seek=None, formats="mov,mp3,wav"):
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
            formats,
            *(["-ss", str(seek)] if seek else []),
            "-i",
            str(path),
            *map(str, output),
        ],
        timeout,
    )


def _encode_loop(source, target, edge, crf):
    try:
        ffmpeg(
            source,
            "-map",
            "0:v:0",
            "-an",
            "-t",
            str(LOOP_SECONDS),
            "-vf",
            f"scale={edge}:{edge}:force_original_aspect_ratio=decrease,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-movflags",
            "+faststart",
            target,
            timeout=25,
            formats="mov,mp4,gif",
        )
    except HTTPException:
        return None
    if not target.exists() or target.stat().st_size < 32:
        return None
    return target.read_bytes()


def playable_clip(data: bytes) -> bytes | None:
    """A post's full mp4, kept for playback with sound when it fits the upload limits:
    H.264, at most five minutes and 25 MiB. Anything else returns None and the post keeps
    only its loop."""
    if not data or len(data) > MAX_BYTES:
        return None
    with tempfile.TemporaryDirectory(prefix="queep-clip-") as folder:
        source = Path(folder) / "clip.mp4"
        source.write_bytes(data)
        try:
            _, video, _ = probe(source)
        except HTTPException:
            return None
    return data if video else None


def preview_loop(data: bytes) -> bytes | None:
    """Muted H.264 gallery preview: the whole GIF, or the first 8 seconds of a video."""
    if not data or len(data) < 32 or len(data) > MAX_BYTES:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="queep-loop-") as folder:
            source = Path(folder) / "input"
            source.write_bytes(data)
            encoded = _encode_loop(source, Path(folder) / "loop.mp4", LOOP_EDGE, 28)
            if encoded is None or len(encoded) > MAX_LOOP_BYTES:
                encoded = _encode_loop(source, Path(folder) / "loop-small.mp4", 640, 33)
            if not encoded or len(encoded) > MAX_LOOP_BYTES:
                return None
            return encoded
    except (OSError, ValueError):
        return None


@dataclass
class PreparedMedia:
    original: bytes
    thumbnail: bytes | None
    info: MediaInfo
    loop: bytes | None = None


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
                "scale=1440:1440:force_original_aspect_ratio=decrease",
                "-q:v",
                "3",
                thumb,
            )
            thumbnail = thumb.read_bytes()
        loop = preview_loop(original) if video else None
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
                loop=bool(loop),
            ),
            loop,
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

    def upload(self, uid, ref_id, title, data, space_ids=()):
        from queep.billing import Billing
        from queep.config import Settings
        Billing(self.db, Settings()).require(uid, "media_av")
        return super().upload(uid, ref_id, title, data, space_ids)

    def kind(self, media):
        return "video" if media.info.content_type == "video/mp4" else "audio"

    def fields(self, media):
        return {"media": media.info.model_dump()}

    def objects(self, media):
        result = [("original", media.original, media.info.content_type)]
        if media.thumbnail:
            result.append(("thumbnail", media.thumbnail, "image/jpeg"))
        if media.loop:
            result.append(("loop", media.loop, "video/mp4"))
        return result

    def read(self, uid, ref_id, thumbnail=False, loop=False):
        reference = self.repo.get(uid, ref_id)
        if not reference.media or ((thumbnail or loop) and reference.kind != "video"):
            raise HTTPException(404, "Mídia não encontrada")
        prefix = self.prefix(uid, ref_id, reference.media.sha256)
        if loop:
            if not reference.media.loop:
                raise HTTPException(404, "Mídia não encontrada")
            return self._download(f"{prefix}/loop"), "video/mp4"
        name = "thumbnail" if thumbnail else "original"
        path = f"{prefix}/{name}"
        if thumbnail:
            data = self._download(path)
            content_type = "image/jpeg"
        else:
            try:
                data = self.bucket.blob(path).download_as_bytes(timeout=30, retry=None)
            except NotFound as error:
                raise HTTPException(404, "Mídia não encontrada") from error
            except GoogleAPICallError as error:
                raise HTTPException(503, "Não foi possível carregar a mídia") from error
            content_type = reference.media.content_type
        if not thumbnail:
            return data, content_type
        video_edge = max(reference.media.width or 0, reference.media.height or 0)
        if thumbnail and video_edge > SHARP_EDGE and preview_edge(data) < SHARP_EDGE:
            sharp = self._poster(prefix)
            if sharp:
                return sharp, "image/jpeg"
        return data, "image/jpeg" if thumbnail else reference.media.content_type

    def _poster(self, prefix):
        """Return a stored sharp poster. Build a missing one without holding the gallery request."""
        card_path = f"{prefix}/card"
        cached = recall_bytes(card_path)
        if cached is not None:
            return cached
        blob = self.bucket.blob(card_path)
        try:
            data = blob.download_as_bytes(timeout=8, retry=None)
        except NotFound:
            self._schedule_poster(prefix)
            return None
        except GoogleAPICallError:
            return None
        remember_bytes(card_path, data)
        return data

    def _schedule_poster(self, prefix):
        if not _preview_slots.acquire(blocking=False):
            return

        def build():
            try:
                card_path = f"{prefix}/card"
                if recall_bytes(card_path):
                    return
                try:
                    source = self.bucket.blob(f"{prefix}/original")
                    card = poster_from_video(source.download_as_bytes(timeout=30, retry=None))
                except (HTTPException, GoogleAPICallError, OSError):
                    return
                try:
                    self.bucket.blob(card_path).upload_from_string(
                        card, content_type="image/jpeg", timeout=20
                    )
                except GoogleAPICallError:
                    pass
                remember_bytes(card_path, card)
            finally:
                _preview_slots.release()

        threading.Thread(target=build, name="queep-poster", daemon=True).start()
