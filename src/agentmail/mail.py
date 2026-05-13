"""Mail object definition and serialization.

Every message is a Mail — a self-contained envelope with cryptographic identity.
"""

from __future__ import annotations

import hashlib
import uuid
import uuid6
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class ContentType(str, Enum):
    """Supported content types for Mail bodies."""

    TEXT_PLAIN = "text/plain"
    APPLICATION_JSON = "application/json"
    APPLICATION_OCTET_STREAM = "application/octet-stream"
    MULTIPART_MIXED = "multipart/mixed"


class Mail(BaseModel):
    """A single Mail message — the core data structure of AgentMail.

    The ``full_hash`` is computed over the envelope (header + body, excluding the
    ``full_hash`` field itself) and serves as the canonical permanent identifier.
    The short hash (``full_hash[:16]``) is used for interactive display and indexing.
    """

    from_addr: str = Field(..., description="Full address of the sender")
    to_addr: str = Field(..., description="Full address of the recipient")
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    id: str = Field(default_factory=lambda: str(uuid6.uuid7()))
    content_type: ContentType = Field(default=ContentType.TEXT_PLAIN)
    message: str = Field(..., description="The payload body")
    full_hash: str = Field(default="", description="SHA-256 of the envelope (auto-computed)")

    @field_validator("at", mode="before")
    @classmethod
    def _ensure_utc(cls, v: Any) -> Any:
        if isinstance(v, str):
            return datetime.fromisoformat(v)
        return v

    def model_post_init(self, __context: Any) -> None:
        """Auto-compute full_hash if not provided."""
        if not self.full_hash:
            self.full_hash = self._compute_hash()

    @property
    def short_hash(self) -> str:
        """Convenience accessor for the first 16 hex chars of full_hash."""
        return self.full_hash[:16]

    def _compute_hash(self) -> str:
        """Compute SHA-256 digest over the envelope minus the hash field."""
        payload = (
            f"{self.from_addr}\n"
            f"{self.to_addr}\n"
            f"{self.at.isoformat()}\n"
            f"{self.id}\n"
            f"{self.content_type.value}\n"
            f"{self.message}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def verify_hash(self) -> bool:
        """Re-compute hash and compare — integrity check."""
        return self._compute_hash() == self.full_hash

    def to_dict(self) -> dict[str, str]:
        """Serialize to a flat dict suitable for JSON storage or API responses."""
        return {
            "from": self.from_addr,
            "to": self.to_addr,
            "at": self.at.isoformat(),
            "id": self.id,
            "content-type": self.content_type.value,
            "message": self.message,
            "full_hash": self.full_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "Mail":
        """Deserialize from a flat dict (API response or stored Mail)."""
        return cls(
            from_addr=data["from"],
            to_addr=data["to"],
            at=data["at"],
            id=data["id"],
            content_type=ContentType(data.get("content-type", "text/plain")),
            message=data["message"],
            full_hash=data.get("full_hash", ""),
        )