from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from PIL import Image, ImageFilter

logger = logging.getLogger(__name__)


def _named_image(data: bytes, name: str = "image.jpg") -> BytesIO:
    stream = BytesIO(data)
    stream.seek(0)
    stream.name = name
    return stream


@dataclass(frozen=True)
class MediaInspection:
    file_id: str
    media_kind: str
    is_image_like: bool
    width: int | None = None
    height: int | None = None
    byte_size: int | None = None
    thumbnail_file_id: str | None = None
    mime_type: str | None = None
    preview_bytes: bytes | None = None


class MediaService:
    def __init__(self, ttl_seconds: int = 900, max_size: int = 512) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self._inspections: dict[str, tuple[MediaInspection, float]] = {}
        self._blurred: dict[str, tuple[bytes, float]] = {}

    async def inspect(
        self,
        bot: Any,
        file_id: str | None,
        media_kind: str | None,
        thumbnail_file_id: str | None = None,
        mime_type: str | None = None,
        is_animated: bool | None = None,
        is_video: bool | None = None,
    ) -> MediaInspection | None:
        if not file_id or not media_kind:
            return None
        key = f"{media_kind}:{file_id}:{thumbnail_file_id or ''}:{mime_type or ''}:{is_animated}:{is_video}"
        cached = self._get(self._inspections, key)
        if cached is not None:
            return cached

        width = None
        height = None
        byte_size = None
        preview = await self.preview_bytes(
            bot,
            file_id,
            media_kind,
            thumbnail_file_id,
            mime_type=mime_type,
            is_animated=is_animated,
            is_video=is_video,
        )
        is_image_like = preview is not None
        if preview is not None:
            try:
                byte_size = len(preview)
                with Image.open(BytesIO(preview)) as image:
                    width, height = image.size
            except Exception:
                is_image_like = False

        inspection = MediaInspection(
            file_id=file_id,
            media_kind=media_kind,
            is_image_like=is_image_like,
            width=width,
            height=height,
            byte_size=byte_size,
            thumbnail_file_id=thumbnail_file_id,
            mime_type=mime_type,
            preview_bytes=preview,
        )
        self._set(
            self._inspections,
            key,
            MediaInspection(
                file_id=file_id,
                media_kind=media_kind,
                is_image_like=is_image_like,
                width=width,
                height=height,
                byte_size=byte_size,
                thumbnail_file_id=thumbnail_file_id,
                mime_type=mime_type,
                preview_bytes=None,
            ),
        )
        return inspection

    async def blur_photo(self, bot: Any, file_id: str) -> BytesIO | None:
        return await self.blur_image(bot, file_id)

    async def preview_bytes(
        self,
        bot: Any,
        file_id: str | None,
        media_kind: str | None,
        thumbnail_file_id: str | None = None,
        mime_type: str | None = None,
        is_animated: bool | None = None,
        is_video: bool | None = None,
    ) -> bytes | None:
        if not file_id or not media_kind:
            return None

        if thumbnail_file_id:
            try:
                raw = await self.fetch_bytes(bot, thumbnail_file_id)
                preview = self._image_preview(raw)
                if preview is not None:
                    return preview
            except Exception:
                logger.debug(
                    "Could not use Telegram thumbnail for media preview file_id=%s kind=%s",
                    file_id,
                    media_kind,
                    exc_info=True,
                )

        if not self._may_download_original_preview(media_kind, mime_type, is_animated, is_video):
            logger.debug(
                "Skipping original media download for preview file_id=%s kind=%s mime_type=%s",
                file_id,
                media_kind,
                mime_type,
            )
            return None

        try:
            raw = await self.fetch_bytes(bot, file_id)
        except Exception:
            logger.debug("Could not fetch media for preview file_id=%s kind=%s", file_id, media_kind, exc_info=True)
            return None

        preview = self._image_preview(raw)
        return preview

    async def blur_image(self, bot: Any, file_id: str) -> BytesIO | None:
        cached = self._get(self._blurred, file_id)
        if cached is not None:
            return _named_image(cached, "blurred.jpg")
        try:
            raw = await self.fetch_bytes(bot, file_id)
            src = BytesIO(raw)
            with Image.open(src) as image:
                image = image.convert("RGB").filter(ImageFilter.GaussianBlur(radius=18))
                out = BytesIO()
                image.save(out, format="JPEG", quality=82)
                data = out.getvalue()
                self._set(self._blurred, file_id, data)
                return _named_image(data, "blurred.jpg")
        except Exception:
            return None

    async def fetch_bytes(self, bot: Any, file_id: str) -> bytes:
        logger.debug("Downloading media bytes for file_id=%s", file_id)
        tg_file = await bot.get_file(file_id)
        return bytes(await tg_file.download_as_bytearray())

    def release_media(
        self,
        file_id: str | None,
        media_kind: str | None,
        thumbnail_file_id: str | None = None,
        mime_type: str | None = None,
        is_animated: bool | None = None,
        is_video: bool | None = None,
    ) -> None:
        if not file_id or not media_kind:
            return
        inspection_key = f"{media_kind}:{file_id}:{thumbnail_file_id or ''}:{mime_type or ''}:{is_animated}:{is_video}"
        self._inspections.pop(inspection_key, None)
        self._blurred.pop(file_id, None)
        if thumbnail_file_id:
            self._blurred.pop(thumbnail_file_id, None)

    def _image_preview(self, raw: bytes) -> bytes | None:
        try:
            with Image.open(BytesIO(raw)) as image:
                try:
                    image.seek(0)
                except EOFError:
                    pass
                image = image.convert("RGB")
                out = BytesIO()
                image.save(out, format="JPEG", quality=88)
                return out.getvalue()
        except Exception:
            return None

    def _may_download_original_preview(
        self,
        media_kind: str,
        mime_type: str | None,
        is_animated: bool | None,
        is_video: bool | None,
    ) -> bool:
        mime = (mime_type or "").lower()
        if media_kind == "photo":
            return True
        if media_kind == "document":
            return mime.startswith("image/")
        if media_kind == "animation":
            return mime == "image/gif"
        if media_kind == "sticker":
            return not bool(is_animated) and not bool(is_video)
        return False

    def _get(self, cache: dict[str, tuple[Any, float]], key: str) -> Any | None:
        self._evict(cache)
        item = cache.get(key)
        if item is None:
            return None
        return item[0]

    def _set(self, cache: dict[str, tuple[Any, float]], key: str, value: Any) -> None:
        cache[key] = (value, time.time())
        self._evict(cache)
        while len(cache) > self.max_size:
            cache.pop(next(iter(cache)))

    def _evict(self, cache: dict[str, tuple[Any, float]]) -> None:
        cutoff = time.time() - self.ttl_seconds
        for key, (_, created_at) in list(cache.items()):
            if created_at < cutoff:
                cache.pop(key, None)
