"""Inbound media handling: images, stickers, GIFs, video and audio.

Output is always text; this module only makes attachments legible to the model.
Videos become a few sampled frames and audio is transcoded to mono mp3 via ffmpeg,
with a Telegram thumbnail fallback when ffmpeg is unavailable.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from telegram import Message

from .config import Settings

log = logging.getLogger(__name__)

MAX_IMAGE_PARTS = 6
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

try:
    from PIL import Image

    PILLOW = True
except ImportError:
    PILLOW = False

PLACEHOLDERS = {
    "photo": "[sent a photo]",
    "sticker": "[sent a sticker]",
    "animation": "[sent a GIF]",
    "video": "[sent a video]",
    "video_note": "[sent a round video message]",
    "voice": "[sent a voice message]",
    "audio": "[sent an audio file]",
}


@dataclass
class MediaBundle:
    parts: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    placeholder: str = ""
    kind: str = ""

    @property
    def has_content(self) -> bool:
        return bool(self.parts)


def ffmpeg_available() -> bool:
    return bool(FFMPEG)


def detect(message: Message) -> tuple[str, object | None]:
    """Return (kind, media object); an empty kind means there is no media."""
    if message.photo:
        return "photo", message.photo[-1]
    if message.sticker:
        return "sticker", message.sticker
    if message.animation:
        return "animation", message.animation
    if message.video:
        return "video", message.video
    if message.video_note:
        return "video_note", message.video_note
    if message.voice:
        return "voice", message.voice
    if message.audio:
        return "audio", message.audio
    if message.document:
        mime = (message.document.mime_type or "").lower()
        if mime.startswith("image/"):
            return "photo", message.document
        if mime.startswith("video/"):
            return "video", message.document
        if mime.startswith("audio/"):
            return "audio", message.document
    return "", None


# -- blocking helpers, run in a worker thread ----------------------------
def _encode_image(path: str, max_dim: int, quality: int) -> str | None:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        if not PILLOW:
            return "data:image/jpeg;base64," + base64.b64encode(raw).decode()

        with Image.open(io.BytesIO(raw)) as img:
            img = img.convert("RGB")
            if max(img.size) > max_dim:
                ratio = max_dim / max(img.size)
                img = img.resize(
                    (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                    Image.LANCZOS,
                )
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:
        log.warning("image encoding failed: %s", exc)
        return None


def _duration(path: str) -> float:
    if not FFPROBE:
        return 0.0
    try:
        out = subprocess.run(  # noqa: S603 - argv list, no shell
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=25,
        )
        return float((out.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def _frames(path: str, workdir: str, count: int, max_dim: int, quality: int) -> list[str]:
    if not FFMPEG:
        return []
    count = max(1, min(count, MAX_IMAGE_PARTS))
    duration = _duration(path)
    timestamps = [0.0] if duration <= 0.2 else [duration * (i + 0.5) / count for i in range(count)]

    encoded: list[str] = []
    for index, offset in enumerate(timestamps):
        out_path = os.path.join(workdir, f"frame_{index}.jpg")
        try:
            subprocess.run(  # noqa: S603 - argv list, no shell
                [FFMPEG, "-nostdin", "-y", "-ss", f"{offset:.2f}", "-i", path,
                 "-frames:v", "1", "-q:v", "3", out_path],
                capture_output=True, timeout=45,
            )
        except Exception as exc:
            log.warning("frame extraction failed: %s", exc)
            continue
        if os.path.exists(out_path) and os.path.getsize(out_path):
            data_url = _encode_image(out_path, max_dim, quality)
            if data_url:
                encoded.append(data_url)
    return encoded


def _to_mp3(path: str, workdir: str, max_seconds: int) -> str | None:
    if not FFMPEG:
        return None
    out_path = os.path.join(workdir, "audio.mp3")
    try:
        subprocess.run(  # noqa: S603 - argv list, no shell
            [FFMPEG, "-nostdin", "-y", "-i", path, "-t", str(max_seconds),
             "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", out_path],
            capture_output=True, timeout=120,
        )
    except Exception as exc:
        log.warning("audio transcode failed: %s", exc)
        return None
    if not os.path.exists(out_path) or not os.path.getsize(out_path):
        return None
    with open(out_path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


# -- entry point ---------------------------------------------------------
async def collect(bot, message: Message, settings: Settings) -> MediaBundle:
    kind, obj = detect(message)
    if not kind or obj is None:
        return MediaBundle()

    bundle = MediaBundle(kind=kind, placeholder=PLACEHOLDERS.get(kind, "[sent a file]"))
    if not settings.media_enabled:
        bundle.notes.append("Media analysis is disabled in the bot configuration.")
        return bundle

    size = getattr(obj, "file_size", None) or 0
    if size > settings.max_media_bytes:
        bundle.notes.append(
            f"The {kind} is too large to download ({size // (1024 * 1024)} MB) and was not seen."
        )
        return bundle

    duration = getattr(obj, "duration", None)
    if kind in {"voice", "audio"} and duration and duration > settings.max_audio_seconds:
        bundle.notes.append(
            f"The audio is {duration}s long; only the first "
            f"{settings.max_audio_seconds}s were heard."
        )

    if kind == "sticker" and getattr(obj, "is_animated", False):
        emoji = getattr(obj, "emoji", "") or ""
        bundle.notes.append(
            f"Animated Lottie sticker with emoji {emoji!r}; it cannot be rendered."
        )
        return bundle

    try:
        with tempfile.TemporaryDirectory(prefix="astolfo_") as workdir:
            source = os.path.join(workdir, "input.bin")
            tg_file = await bot.get_file(obj.file_id)
            await tg_file.download_to_drive(custom_path=source)
            await _process(kind, obj, source, workdir, bundle, settings, bot)
    except Exception as exc:
        log.warning("could not process %s: %s", kind, exc)
        bundle.notes.append("The attachment could not be opened, so nothing was seen or heard.")
    return bundle


async def _process(kind, obj, source, workdir, bundle, settings: Settings, bot) -> None:
    video_sticker = kind == "sticker" and getattr(obj, "is_video", False)

    if kind == "photo" or (kind == "sticker" and not video_sticker):
        data_url = await asyncio.to_thread(
            _encode_image, source, settings.image_max_dim, settings.image_quality
        )
        if data_url:
            bundle.parts.append({"type": "image_url", "image_url": {"url": data_url}})
            if kind == "sticker":
                emoji = getattr(obj, "emoji", "") or ""
                bundle.notes.append(
                    "This is a Telegram sticker" + (f" with emoji {emoji!r}." if emoji else ".")
                )
        else:
            bundle.notes.append("The image could not be read.")
        return

    if kind in {"animation", "video", "video_note"} or video_sticker:
        frames = await asyncio.to_thread(
            _frames, source, workdir, settings.video_frames,
            settings.image_max_dim, settings.image_quality,
        )
        if frames:
            bundle.parts.extend(
                {"type": "image_url", "image_url": {"url": frame}} for frame in frames
            )
            label = "GIF" if kind == "animation" or video_sticker else "video"
            seconds = getattr(obj, "duration", None)
            bundle.notes.append(
                f"{len(frames)} sampled frames from a {label}"
                + (f" about {seconds}s long" if seconds else "")
                + ", in chronological order. The video's audio is not available."
            )
            return

        thumb = getattr(obj, "thumbnail", None) or getattr(obj, "thumb", None)
        if thumb is not None:
            with contextlib.suppress(Exception):
                thumb_path = os.path.join(workdir, "thumb.jpg")
                tg_thumb = await bot.get_file(thumb.file_id)
                await tg_thumb.download_to_drive(custom_path=thumb_path)
                data_url = await asyncio.to_thread(
                    _encode_image, thumb_path, settings.image_max_dim, settings.image_quality
                )
                if data_url:
                    bundle.parts.append({"type": "image_url", "image_url": {"url": data_url}})
                    bundle.notes.append(
                        "Only the video thumbnail was available (ffmpeg is not installed), "
                        "so this is a single still frame rather than the whole video."
                    )
                    return
        bundle.notes.append("The video could not be opened, so its content was not seen.")
        return

    if kind in {"voice", "audio"}:
        encoded = await asyncio.to_thread(_to_mp3, source, workdir, settings.max_audio_seconds)
        if encoded:
            bundle.parts.append(
                {"type": "input_audio", "input_audio": {"data": encoded, "format": "mp3"}}
            )
            title = getattr(obj, "title", None)
            if title:
                bundle.notes.append(f"Audio file title: {title}")
        else:
            bundle.notes.append(
                "This voice message could not be decoded (ffmpeg is unavailable). Say honestly "
                "that you could not hear it and ask them to type it."
            )
        return

    bundle.notes.append("This file type is not supported.")
