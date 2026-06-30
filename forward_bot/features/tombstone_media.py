from __future__ import annotations

from io import BytesIO

from PIL import Image
from telegram import InputFile, InputMediaPhoto


def _build_removed_png() -> bytes:
    image = Image.new("RGB", (64, 64), (230, 230, 230))
    for x in range(64):
        for y in range(64):
            if x in (0, 63) or y in (0, 63) or abs(x - y) <= 1 or abs(x + y - 63) <= 1:
                image.putpixel((x, y), (120, 120, 120))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


_REMOVED_PNG = _build_removed_png()


def removed_photo_media(caption: str = "Message removed.") -> InputMediaPhoto:
    return InputMediaPhoto(
        media=InputFile(BytesIO(_REMOVED_PNG), filename="removed.png", attach=True),
        caption=caption,
        parse_mode="HTML",
    )
