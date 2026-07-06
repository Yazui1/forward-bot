from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from PIL import Image, ImageFilter
from telegram import Bot, Message


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class MediaInspection:
    preview_bytes: bytes | None = None
    width: int | None = None
    height: int | None = None
    byte_size: int | None = None
    image_like: bool = False
    empty_preview: bool = False


class MediaService:
    def __init__(self) -> None:
        self._preview_cache: dict[int, bytes] = {}
        self._blur_cache: dict[int, bytes] = {}
        self._blur_file_id_cache: dict[int, str] = {}
        self._blur_upload_locks: dict[int, asyncio.Lock] = {}

    async def inspect(self, bot: Bot, message_id: int, payload: dict[str, Any]) -> MediaInspection:
        file_id = _preview_file_id(payload)
        if not file_id:
            return MediaInspection()
        content_type = payload.get("content_type")
        try:
            tg_file = await bot.get_file(file_id)
            data = bytes(await tg_file.download_as_bytearray())
        except Exception:
            return MediaInspection(empty_preview=True)
        self._preview_cache[message_id] = data
        width = height = None
        image_like = False
        try:
            with Image.open(BytesIO(data)) as img:
                width, height = img.size
                image_like = True
        except Exception:
            image_like = content_type in {"photo", "sticker"}
        return MediaInspection(
            preview_bytes=data,
            width=width,
            height=height,
            byte_size=len(data),
            image_like=image_like,
            empty_preview=not bool(data),
        )

    async def blurred_preview(self, message_id: int) -> bytes | None:
        if message_id in self._blur_cache:
            return self._blur_cache[message_id]
        data = self._preview_cache.get(message_id)
        if not data:
            return None
        try:
            with Image.open(BytesIO(data)) as img:
                img = img.convert("RGB").filter(ImageFilter.GaussianBlur(radius=8))
                out = BytesIO()
                img.save(out, format="JPEG", quality=70)
                blurred = out.getvalue()
                self._blur_cache[message_id] = blurred
                return blurred
        except Exception:
            return None

    def blurred_file_id(self, message_id: int) -> str | None:
        return self._blur_file_id_cache.get(message_id)

    def set_blurred_file_id(self, message_id: int, file_id: str) -> None:
        self._blur_file_id_cache[message_id] = file_id

    def blur_upload_lock(self, message_id: int) -> asyncio.Lock:
        lock = self._blur_upload_locks.get(message_id)
        if lock is None:
            lock = asyncio.Lock()
            self._blur_upload_locks[message_id] = lock
        return lock

    def release(self, message_id: int) -> None:
        self._preview_cache.pop(message_id, None)
        self._blur_cache.pop(message_id, None)
        self._blur_file_id_cache.pop(message_id, None)
        self._blur_upload_locks.pop(message_id, None)


class AIClassifier:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._model: Any = None
        self._model_path: str | None = None
        self._lock = asyncio.Lock()

    def update_config(self, config: dict[str, Any]) -> None:
        if config.get("model_path") != self._model_path:
            self._model = None
            self._model_path = None
        self.config = config

    async def classify(self, preview_bytes: bytes | None) -> tuple[str, str | None]:
        if not self.config.get("enabled") or not preview_bytes:
            return "OK", None
        timeout = float(self.config.get("timeout_seconds", 2.0) or 2.0)
        try:
            score = await asyncio.wait_for(self._predict_score(preview_bytes), timeout=timeout)
        except Exception as exc:
            LOGGER.debug("AI classifier failed open: %s", exc)
            return "OK", None
        block_threshold = self.config.get("block_threshold")
        question_threshold = self.config.get("question_threshold")
        if block_threshold is not None and score >= float(block_threshold):
            return "BLOCKED", f"AI score:{score:.4f}"
        if question_threshold is not None and score >= float(question_threshold):
            return "QUESTIONABLE", f"AI score:{score:.4f}"
        return "OK", None

    async def _predict_score(self, preview_bytes: bytes) -> float:
        async with self._lock:
            model = await self._load_model()
            if model is None:
                return 0.0
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._predict_sync, model, preview_bytes)

    async def _load_model(self) -> Any:
        model_path = str(self.config.get("model_path") or "")
        if self._model is not None and self._model_path == model_path:
            return self._model
        if not model_path:
            return None
        loop = asyncio.get_running_loop()
        self._model = await loop.run_in_executor(None, _load_keras_model, model_path)
        self._model_path = model_path
        return self._model

    def _predict_sync(self, model: Any, preview_bytes: bytes) -> float:
        import numpy as np

        size = int(self.config.get("image_size", 224) or 224)
        with Image.open(BytesIO(preview_bytes)) as img:
            img = img.convert("RGB").resize((size, size))
            arr = np.asarray(img, dtype="float32") / 255.0
        pred = model.predict(arr[None, ...], verbose=0)
        return float(np.asarray(pred).reshape(-1)[0])


def _load_keras_model(model_path: str) -> Any:
    try:
        from tensorflow import keras
        return keras.models.load_model(model_path)
    except Exception:
        try:
            import keras
            return keras.models.load_model(model_path)
        except Exception as exc:
            raise RuntimeError(f"failed to load AI model {model_path}: {exc}") from exc


def extract_payload(message: Message) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "content_type": "text",
        "text": message.text or message.caption or "",
        "media_file_id": None,
        "thumbnail_file_id": None,
        "media_kind": None,
        "mime_type": None,
        "sticker_set_name": None,
        "is_animated": False,
        "is_video": False,
        "parse_mode": None,
        "forward_from_chat_id": None,
        "forward_from_message_id": None,
    }
    origin = getattr(message, "forward_origin", None)
    origin_name = origin.__class__.__name__.lower() if origin else ""
    if origin and not getattr(message, "has_protected_content", False) and "hidden" not in origin_name:
        payload["forward_from_chat_id"] = message.chat_id
        payload["forward_from_message_id"] = message.message_id
    if message.photo:
        largest = message.photo[-1]
        payload.update(
            content_type="photo",
            media_file_id=largest.file_id,
            thumbnail_file_id=message.photo[0].file_id if len(message.photo) > 1 else largest.file_id,
            media_kind="photo",
            text=message.caption or "",
        )
    elif message.video:
        payload.update(
            content_type="video",
            media_file_id=message.video.file_id,
            thumbnail_file_id=message.video.thumbnail.file_id if message.video.thumbnail else None,
            media_kind="video",
            mime_type=message.video.mime_type,
            text=message.caption or "",
        )
    elif message.animation:
        payload.update(
            content_type="animation",
            media_file_id=message.animation.file_id,
            thumbnail_file_id=message.animation.thumbnail.file_id if message.animation.thumbnail else None,
            media_kind="animation",
            mime_type=message.animation.mime_type,
            text=message.caption or "",
        )
    elif message.sticker:
        payload.update(
            content_type="sticker",
            media_file_id=message.sticker.file_id,
            thumbnail_file_id=message.sticker.thumbnail.file_id if message.sticker.thumbnail else None,
            media_kind="sticker",
            sticker_set_name=message.sticker.set_name,
            is_animated=bool(message.sticker.is_animated),
            is_video=bool(message.sticker.is_video),
            text="",
        )
    elif message.document:
        payload.update(
            content_type="document",
            media_file_id=message.document.file_id,
            thumbnail_file_id=message.document.thumbnail.file_id if message.document.thumbnail else None,
            media_kind="document",
            mime_type=message.document.mime_type,
            text=message.caption or "",
        )
    elif message.video_note:
        payload.update(
            content_type="video_note",
            media_file_id=message.video_note.file_id,
            thumbnail_file_id=message.video_note.thumbnail.file_id if message.video_note.thumbnail else None,
            media_kind="video_note",
            text="",
        )
    elif message.text:
        payload["text"] = message.text
    return payload


def _preview_file_id(payload: dict[str, Any]) -> str | None:
    content_type = payload.get("content_type")
    thumbnail = payload.get("thumbnail_file_id")
    media = payload.get("media_file_id")
    if content_type in {"video", "animation", "video_note", "document"}:
        return thumbnail
    if content_type == "sticker" and (payload.get("is_animated") or payload.get("is_video")):
        return thumbnail
    return thumbnail or media
