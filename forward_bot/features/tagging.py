from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from forward_bot.config import Config
from forward_bot.db.repository import Repository
from forward_bot.features.duplicate_media import media_digest
from forward_bot.features.media import AIClassifier, MediaInspection
from forward_bot.utils import now_utc, parse_dt


TAG_OK = "OK"
TAG_BLOCKED = "BLOCKED"
TAG_QUESTIONABLE = "QUESTIONABLE"
TAG_POTENTIALLY_UNWANTED = "POTENTIALLY_UNWANTED"
TAG_DUPLICATE = "DUPLICATE"

_INVITE_RE = re.compile(r"https?://(?:t\.me|telegram\.me)/([A-Za-z0-9_+/.-]+)|(?:^|\s)(?:t\.me|telegram\.me)/([A-Za-z0-9_+/.-]+)", re.I)
_EXCLUDED_INVITE_PREFIXES = ("c/", "s/", "share/", "addstickers/", "iv", "BotFather")


@dataclass(slots=True)
class TagResult:
    tag: str = TAG_OK
    reason: str | None = None
    media_hash: str | None = None
    media_hash_first_seen_at: str | None = None
    remove_buttons: bool = False


class TaggingPipeline:
    def __init__(self, config: Config, repo: Repository, ai: AIClassifier):
        self.config = config
        self.repo = repo
        self.ai = ai
        self.refresh(config)

    def refresh(self, config: Config) -> None:
        self.config = config
        self.blocked_terms = [str(x).lower() for x in config.get("tagging.blocked_terms", []) or []]
        self.questionable_terms = [str(x).lower() for x in config.get("tagging.questionable_terms", []) or []]
        self.potentially_unwanted_terms = [str(x).lower() for x in config.get("tagging.potentially_unwanted_terms", []) or []]

    async def classify(
        self,
        payload: dict[str, Any],
        inspection: MediaInspection,
        *,
        include_duplicates: bool = True,
    ) -> TagResult:
        result = TagResult()
        text = (payload.get("text") or "").lower()

        sticker_set = payload.get("sticker_set_name")
        if sticker_set and self.repo.get_blocked_sticker_set(sticker_set):
            return TagResult(TAG_BLOCKED, "blocked-sticker-set")

        invite = self._invite_result(payload.get("text") or "")
        if invite:
            if invite == "blocked":
                return TagResult(TAG_BLOCKED, "telegram-invite-undescribed")
            result.reason = "telegram-invite-described"
            result.remove_buttons = True

        if result.tag == TAG_OK:
            for term in self.potentially_unwanted_terms:
                if term and term in text:
                    result.tag = TAG_POTENTIALLY_UNWANTED
                    result.reason = f"potentially-unwanted-term:{term}"
                    break

        for term in self.blocked_terms:
            if term and term in text:
                return TagResult(TAG_BLOCKED, f"blocked-term:{term}")

        if result.tag in {TAG_OK, TAG_POTENTIALLY_UNWANTED}:
            for term in self.questionable_terms:
                if term and term in text:
                    result.tag = TAG_QUESTIONABLE
                    result.reason = f"questionable-term:{term}"
                    break

        if inspection.image_like and inspection.preview_bytes:
            ai_tag, ai_reason = await self.ai.classify(inspection.preview_bytes)
            if ai_tag == TAG_BLOCKED:
                return TagResult(TAG_BLOCKED, ai_reason or "ai-blocked")
            if ai_tag == TAG_QUESTIONABLE and result.tag == TAG_OK:
                result.tag = TAG_QUESTIONABLE
                result.reason = ai_reason or "ai-questionable"

        if include_duplicates:
            self.classify_duplicate(payload, inspection, result)
        return result

    def classify_duplicate(
        self,
        payload: dict[str, Any],
        inspection: MediaInspection,
        result: TagResult,
    ) -> TagResult:
        if self.config.get("duplicates.enabled", True) and inspection.image_like and payload.get("content_type") != "sticker":
            digest = media_digest(inspection.preview_bytes)
            if digest:
                existing = self.repo.get_media_hash(digest)
                if existing:
                    result.media_hash = digest
                    result.media_hash_first_seen_at = existing.get("first_seen_at")
                    first = parse_dt(existing.get("first_seen_at"))
                    retention = int(self.config.get("duplicates.sender_block_retention_days", 3) or 3)
                    if first and first + timedelta(days=retention) >= now_utc():
                        result.tag = TAG_DUPLICATE
                        result.reason = "duplicate-media"
                    self.repo.upsert_media_hash(digest, first_seen_at=existing.get("first_seen_at"))
                else:
                    info = self.repo.upsert_media_hash(digest)
                    result.media_hash = digest
                    result.media_hash_first_seen_at = info.get("first_seen_at")
        return result

    def _invite_result(self, text: str) -> str | None:
        for match in _INVITE_RE.finditer(text):
            target = (match.group(1) or match.group(2) or "").lstrip("/")
            if not target or target.startswith(_EXCLUDED_INVITE_PREFIXES):
                continue
            remaining = (text[: match.start()] + text[match.end() :]).strip()
            meaningful = re.sub(r"\W+", "", remaining)
            return "described" if len(meaningful) >= 3 else "blocked"
        return None
