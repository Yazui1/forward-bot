from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from PIL import Image

from forward_bot.features.duplicate_media import compute_media_hash

logger = logging.getLogger(__name__)

TELEGRAM_INVITE_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?!c/|s/|share/|addstickers/|iv\\?|botfather\\b)[A-Za-z0-9_+][A-Za-z0-9_+/?=&.-]*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TagResult:
    tag: str
    reason: str | None = None


@dataclass
class AIClassifier:
    enabled: bool = False
    model_path: str = "model.keras"
    question_threshold: float = 0.93
    block_threshold: float | None = None
    image_size: int = 224

    _model: Any | None = None
    _load_attempted: bool = False

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "AIClassifier":
        ai_cfg = cfg.get("ai", {})
        return cls(
            enabled=bool(ai_cfg.get("enabled", False)),
            model_path=str(ai_cfg.get(
                "model_path", "model.keras")),
            question_threshold=float(ai_cfg.get("question_threshold", 0.93)),
            block_threshold=(
                float(ai_cfg["block_threshold"])
                if ai_cfg.get("block_threshold") is not None
                else None
            ),
            image_size=int(ai_cfg.get("image_size", 224)),
        )

    def update_config(self, cfg: dict[str, Any]) -> None:
        ai_cfg = cfg.get("ai", {})
        model_path = str(ai_cfg.get("model_path", self.model_path))
        if model_path != self.model_path:
            self._model = None
            self._load_attempted = False
        self.enabled = bool(ai_cfg.get("enabled", self.enabled))
        self.model_path = model_path
        self.question_threshold = float(ai_cfg.get(
            "question_threshold", self.question_threshold))
        self.block_threshold = (
            float(ai_cfg["block_threshold"])
            if ai_cfg.get("block_threshold") is not None
            else None
        )
        self.image_size = int(ai_cfg.get("image_size", self.image_size))

    def classify_image(self, image_bytes: bytes) -> TagResult:
        if not self.enabled:
            return TagResult("OK")
        model = self._load_model()
        if model is None:
            return TagResult("OK")
        try:
            arr = self._preprocess(image_bytes)
            pred = float(model.predict(arr, verbose=0)[0][0])
        except Exception as exc:
            logger.exception("AI media classification failed: %s", exc)
            return TagResult("OK")

        logger.debug("AI media classification prediction=%.4f", pred)
        if self.block_threshold is not None and pred >= self.block_threshold:
            logger.info(
                "AI media classifier blocked image prediction=%.4f", pred)
            return TagResult("BLOCKED", "ai-blocked-media")
        if pred >= self.question_threshold:
            logger.info(
                "AI media classifier marked image questionable prediction=%.4f", pred)
            return TagResult("QUESTIONABLE", "ai-questionable-media")
        return TagResult("OK")

    def _load_model(self) -> Any | None:
        if self._load_attempted:
            return self._model
        self._load_attempted = True
        try:
            import tensorflow as tf  # type: ignore[import-not-found]

            self._model = tf.keras.models.load_model(self.model_path)
            logger.info("Loaded AI media classifier model from %s",
                        self.model_path)
        except Exception as exc:
            self._model = None
            logger.warning(
                "AI media classifier disabled; failed to load model %s: %s", self.model_path, exc)
        return self._model

    def _preprocess(self, image_bytes: bytes) -> Any:
        try:
            import numpy as np  # type: ignore[import-not-found]
        except Exception as exc:
            raise RuntimeError(
                "numpy is required for AI media classification") from exc

        with Image.open(BytesIO(image_bytes)) as image:
            image = image.convert("RGB").resize(
                (self.image_size, self.image_size))
            arr = np.array(image, dtype=np.float32) / 255.0
        return np.expand_dims(arr, axis=0)


class TaggingPipeline:
    def __init__(
        self,
        blocked_terms: list[str],
        questionable_terms: list[str],
        potentially_unwanted_terms: list[str] | None = None,
        ai_classifier: AIClassifier | None = None,
    ) -> None:
        self.blocked_terms = [x.lower() for x in blocked_terms]
        self.questionable_terms = [x.lower() for x in questionable_terms]
        self.potentially_unwanted_terms = [
            x.lower() for x in (potentially_unwanted_terms or [])]
        self.ai_classifier = ai_classifier

    async def run_once(
        self,
        text: str | None,
        media_kind: str | None,
        media_info: object | None = None,
        media_bytes: bytes | None = None,
        repo: Any | None = None,
        cfg: dict[str, Any] | None = None,
        message_id: int | None = None,
    ) -> TagResult:
        result = TagResult("OK")
        if media_kind:
            if media_info is not None and getattr(media_info, "byte_size", None) == 0:
                result = TagResult("QUESTIONABLE", "empty-media")
            ai_result = self._classify_media(
                media_kind, media_info, media_bytes)
            if ai_result is not None:
                result = ai_result

        if text:
            lowered = text.lower()
            invite = self._classify_telegram_invite(text)
            if invite is not None:
                result = invite
            for term in self.potentially_unwanted_terms:
                if term and term in lowered:
                    if result.tag == "OK":
                        result = TagResult("POTENTIALLY_UNWANTED", "potentially-unwanted-term")
                    break
            for term in self.blocked_terms:
                if term and term in lowered:
                    result = TagResult("BLOCKED", "blocked-term")
                    break
            for term in self.questionable_terms:
                if term and term in lowered:
                    if result.tag in {"OK", "POTENTIALLY_UNWANTED"}:
                        result = TagResult("QUESTIONABLE", "questionable-term")
                    break

        duplicate = await self._classify_duplicate_media(
            media_kind,
            media_info,
            media_bytes,
            repo,
            cfg,
            message_id,
        )
        if duplicate is not None:
            return duplicate

        return result

    def _classify_telegram_invite(self, text: str) -> TagResult | None:
        match = TELEGRAM_INVITE_RE.search(text)
        if match is None:
            return None
        remaining = (text[:match.start()] + " " + text[match.end():]).strip()
        if len(remaining) >= 3:
            return TagResult("OK", "telegram-invite-described")
        return TagResult("BLOCKED", "telegram-invite-link")

    def _classify_media(
        self,
        media_kind: str | None,
        media_info: Any,
        media_bytes: bytes | None,
    ) -> TagResult | None:
        if self.ai_classifier is None or not self.ai_classifier.enabled:
            return None
        if media_bytes is None:
            return None
        if not media_kind or media_info is None or not getattr(media_info, "is_image_like", False):
            return None
        result = self.ai_classifier.classify_image(media_bytes)
        if result.tag != "OK":
            return result
        return None

    async def _classify_duplicate_media(
        self,
        media_kind: str | None,
        media_info: Any,
        media_bytes: bytes | None,
        repo: Any | None,
        cfg: dict[str, Any] | None,
        message_id: int | None,
    ) -> TagResult | None:
        if repo is None or cfg is None or message_id is None:
            return None
        if not bool(cfg.get("duplicates", {}).get("enabled", True)):
            return None
        if media_kind == "sticker":
            return None
        if media_bytes is None:
            return None
        if media_info is None or not getattr(media_info, "is_image_like", False):
            return None

        sender_block_days = int(cfg.get("duplicates", {}).get("sender_block_retention_days", 3))
        try:
            hash_value = compute_media_hash(media_bytes)
            first_seen_at = await repo.first_media_hash_seen_at(hash_value)
            if first_seen_at is not None:
                await repo.set_message_media_hash(message_id, hash_value, first_seen_at)
                logger.info(
                    "Duplicate media detected message_id=%s media_kind=%s",
                    message_id,
                    media_kind,
                )
                if await repo.media_hash_seen_within(hash_value, sender_block_days):
                    return TagResult("DUPLICATE", "duplicate-media")
                await repo.add_media_hash(hash_value)
                return None
            await repo.add_media_hash(hash_value)
            await repo.set_message_media_hash(message_id, hash_value, None)
        except Exception:
            logger.exception("Failed duplicate media check message_id=%s", message_id)
        return None
