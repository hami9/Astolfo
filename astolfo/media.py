"""پردازش رسانهٔ ورودی: عکس، استیکر، گیف، ویدیو، ویس و فایل صوتی.

خروجی همیشه فقط متن است؛ این ماژول فقط ورودی را برای مدل قابل‌فهم می‌کند.
ویدیو/گیف به چند فریم تبدیل می‌شود و صدا به mp3 (با ffmpeg). اگر ffmpeg نبود،
از تصویر بندانگشتی تلگرام به‌عنوان جایگزین استفاده می‌شود.
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
from typing import List, Optional, Tuple

from telegram import Message

from .config import Settings

log = logging.getLogger("astolfo.media")

MAX_IMAGE_PARTS = 6
_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")

try:
    from PIL import Image

    _PIL = True
except Exception:  # pragma: no cover
    _PIL = False
    log.warning("Pillow نصب نیست؛ تصاویر بدون فشرده‌سازی ارسال می‌شوند.")


@dataclass
class MediaBundle:
    parts: List[dict] = field(default_factory=list)   # بخش‌های چندوجهی برای مدل
    notes: List[str] = field(default_factory=list)    # توضیح متنی برای مدل
    placeholder: str = ""                             # چیزی که در تاریخچه ذخیره می‌شود
    kind: str = ""                                    # نوع اصلی رسانه

    @property
    def has_content(self) -> bool:
        return bool(self.parts)


def ffmpeg_available() -> bool:
    return bool(_FFMPEG)


# ---------------------------------------------------------------------------
# کمک‌کننده‌های همگام (در ترد جدا اجرا می‌شوند)
# ---------------------------------------------------------------------------
def _encode_image(path: str, max_dim: int) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        if not _PIL:
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
            img.save(buf, format="JPEG", quality=85, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:
        log.warning("کدگذاری تصویر شکست خورد: %s", exc)
        return None


def _probe_duration(path: str) -> float:
    if not _FFPROBE:
        return 0.0
    try:
        out = subprocess.run(
            [
                _FFPROBE, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            capture_output=True, text=True, timeout=25,
        )
        return float((out.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def _extract_frames(path: str, workdir: str, count: int, max_dim: int) -> List[str]:
    """چند فریم نمونه از ویدیو/گیف می‌گیرد و به data-url تبدیل می‌کند."""
    if not _FFMPEG:
        return []
    duration = _probe_duration(path)
    frames: List[str] = []
    count = max(1, min(count, MAX_IMAGE_PARTS))

    if duration <= 0.2:  # مدت نامعلوم یا خیلی کوتاه → فقط فریم اول
        timestamps = [0.0]
    else:
        timestamps = [duration * (i + 0.5) / count for i in range(count)]

    for idx, ts in enumerate(timestamps):
        out_path = os.path.join(workdir, f"frame_{idx}.jpg")
        try:
            subprocess.run(
                [
                    _FFMPEG, "-nostdin", "-y", "-ss", f"{ts:.2f}", "-i", path,
                    "-frames:v", "1", "-q:v", "3", out_path,
                ],
                capture_output=True, timeout=45,
            )
        except Exception as exc:
            log.warning("استخراج فریم شکست خورد: %s", exc)
            continue
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            encoded = _encode_image(out_path, max_dim)
            if encoded:
                frames.append(encoded)
    return frames


def _to_mp3(path: str, workdir: str, max_seconds: int) -> Optional[str]:
    if not _FFMPEG:
        return None
    out_path = os.path.join(workdir, "audio.mp3")
    try:
        subprocess.run(
            [
                _FFMPEG, "-nostdin", "-y", "-i", path, "-t", str(max_seconds),
                "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", out_path,
            ],
            capture_output=True, timeout=120,
        )
    except Exception as exc:
        log.warning("تبدیل صدا شکست خورد: %s", exc)
        return None
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return None
    with open(out_path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


# ---------------------------------------------------------------------------
# تشخیص نوع رسانه در پیام تلگرام
# ---------------------------------------------------------------------------
def detect(message: Message) -> Tuple[str, Optional[object]]:
    """(نوع، شیء رسانه) را برمی‌گرداند؛ نوع خالی یعنی رسانه‌ای نیست."""
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


PLACEHOLDERS = {
    "photo": "[یک عکس فرستاد]",
    "sticker": "[یک استیکر فرستاد]",
    "animation": "[یک گیف فرستاد]",
    "video": "[یک ویدیو فرستاد]",
    "video_note": "[یک ویدیو-پیام گرد فرستاد]",
    "voice": "[یک ویس فرستاد]",
    "audio": "[یک فایل صوتی فرستاد]",
}


# ---------------------------------------------------------------------------
# جمع‌آوری اصلی
# ---------------------------------------------------------------------------
async def collect(bot, message: Message, settings: Settings) -> MediaBundle:
    kind, obj = detect(message)
    if not kind or obj is None:
        return MediaBundle()

    bundle = MediaBundle(kind=kind, placeholder=PLACEHOLDERS.get(kind, "[یک فایل فرستاد]"))

    if not settings.media_enabled:
        bundle.notes.append("تحلیل رسانه در تنظیمات خاموش است.")
        return bundle

    file_size = getattr(obj, "file_size", None) or 0
    if file_size and file_size > settings.max_media_bytes:
        bundle.notes.append(
            f"فایل {kind} برای دانلود خیلی بزرگ است ({file_size // (1024 * 1024)} مگابایت) "
            "و دیده/شنیده نشد."
        )
        return bundle

    duration = getattr(obj, "duration", None)
    if kind in {"voice", "audio"} and duration and duration > settings.max_audio_seconds:
        bundle.notes.append(
            f"صدا {duration} ثانیه است؛ فقط {settings.max_audio_seconds} ثانیهٔ اولش شنیده شد."
        )

    # استیکر متحرک tgs قابل رندر نیست
    if kind == "sticker" and getattr(obj, "is_animated", False):
        emoji = getattr(obj, "emoji", "") or ""
        bundle.notes.append(f"استیکر متحرک (Lottie) با ایموجی «{emoji}» — قابل مشاهده نیست.")
        return bundle

    try:
        with tempfile.TemporaryDirectory(prefix="astolfo_") as workdir:
            src = os.path.join(workdir, "input.bin")
            tg_file = await bot.get_file(obj.file_id)
            await tg_file.download_to_drive(custom_path=src)

            await _process(kind, obj, src, workdir, bundle, settings, bot)
    except Exception as exc:
        log.warning("پردازش رسانه (%s) شکست خورد: %s", kind, exc)
        bundle.notes.append("این فایل باز نشد؛ چیزی ازش دیده/شنیده نشد.")

    return bundle


async def _process(
    kind: str,
    obj,
    src: str,
    workdir: str,
    bundle: MediaBundle,
    settings: Settings,
    bot,
) -> None:
    is_video_sticker = kind == "sticker" and getattr(obj, "is_video", False)

    # ---- تصویر ثابت ----
    if kind == "photo" or (kind == "sticker" and not is_video_sticker):
        data_url = await asyncio.to_thread(_encode_image, src, settings.image_max_dim)
        if data_url:
            bundle.parts.append({"type": "image_url", "image_url": {"url": data_url}})
            if kind == "sticker":
                emoji = getattr(obj, "emoji", "") or ""
                bundle.notes.append(
                    f"این یک استیکر تلگرام است" + (f" با ایموجی «{emoji}»." if emoji else ".")
                )
        else:
            bundle.notes.append("تصویر قابل خواندن نبود.")
        return

    # ---- ویدیو / گیف / ویدیو-پیام / استیکر ویدیویی ----
    if kind in {"animation", "video", "video_note"} or is_video_sticker:
        frames = await asyncio.to_thread(
            _extract_frames, src, workdir, settings.video_frames, settings.image_max_dim
        )
        if frames:
            for frame in frames:
                bundle.parts.append({"type": "image_url", "image_url": {"url": frame}})
            label = "گیف" if kind == "animation" or is_video_sticker else "ویدیو"
            secs = getattr(obj, "duration", None)
            bundle.notes.append(
                f"{len(frames)} فریم نمونه از یک {label}"
                + (f" با طول حدود {secs} ثانیه" if secs else "")
                + " به‌ترتیب زمانی پیوست شده است (صدای ویدیو در دسترس نیست)."
            )
            return

        # جایگزین: تصویر بندانگشتی خود تلگرام
        thumb = getattr(obj, "thumbnail", None) or getattr(obj, "thumb", None)
        if thumb is not None:
            with contextlib.suppress(Exception):
                thumb_path = os.path.join(workdir, "thumb.jpg")
                tg_thumb = await bot.get_file(thumb.file_id)
                await tg_thumb.download_to_drive(custom_path=thumb_path)
                data_url = await asyncio.to_thread(
                    _encode_image, thumb_path, settings.image_max_dim
                )
                if data_url:
                    bundle.parts.append({"type": "image_url", "image_url": {"url": data_url}})
                    bundle.notes.append(
                        "فقط تصویر بندانگشتی این ویدیو در دسترس بود (ffmpeg نصب نیست)، "
                        "پس یک قاب ثابت می‌بینی نه کل ویدیو."
                    )
                    return
        bundle.notes.append("این ویدیو باز نشد؛ محتوایش دیده نشد.")
        return

    # ---- صدا ----
    if kind in {"voice", "audio"}:
        encoded = await asyncio.to_thread(_to_mp3, src, workdir, settings.max_audio_seconds)
        if encoded:
            bundle.parts.append(
                {"type": "input_audio", "input_audio": {"data": encoded, "format": "mp3"}}
            )
            title = getattr(obj, "title", None)
            if title:
                bundle.notes.append(f"عنوان فایل صوتی: {title}")
        else:
            bundle.notes.append(
                "این ویس تبدیل نشد و شنیده نشد (ffmpeg در دسترس نیست) — "
                "صادقانه بگو نشنیدی و ازش بخواه تایپ کنه."
            )
        return

    bundle.notes.append("نوع این فایل پشتیبانی نمی‌شود.")
