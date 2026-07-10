"""File-based retry queue with exponential backoff.

When a transport fails to deliver a Mail, the send is not lost — it is
written to a persistent queue and retried with exponential backoff until
``max_retries`` is reached. This honors the protocol's "queue locally,
never silent-drop" resilience principle.

Layout:
    ~/.agentmail/
    └── queue/
        └── <mail_id>.pending   ← one JSON file per undelivered send
"""

from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .config import DEFAULT_CONFIG_DIR
from .mail import Mail

# Backoff tuning (seconds). delay(n) = min(BASE * 2**n, MAX_DELAY)
BASE_DELAY = 2.0
MAX_DELAY = 300.0  # 5 minutes ceiling


class PendingSend(BaseModel):
    """A single undelivered send awaiting retry."""

    mail: dict[str, Any] = Field(..., description="mail.to_dict() snapshot")
    attempts: int = Field(default=0, description="Number of delivery attempts made")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    next_attempt: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Earliest time this send may be retried",
    )
    last_error: str = Field(default="")

    @property
    def mail_id(self) -> str:
        return self.mail.get("id", "")

    @property
    def full_hash(self) -> str:
        return self.mail.get("full_hash", "")


def _backoff_delay(attempts: int) -> float:
    """Exponential backoff for the given attempt count.

    attempt 0 → BASE, attempt 1 → 2*BASE, attempt 2 → 4*BASE, ... capped.
    """
    return min(BASE_DELAY * (2 ** attempts), MAX_DELAY)


class RetryQueue:
    """Persistent, file-backed queue of undelivered sends."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or DEFAULT_CONFIG_DIR
        self.queue_dir = self.base_dir / "queue"
        self._lock = threading.Lock()

    def _ensure_dir(self) -> None:
        self.queue_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, mail_id: str) -> Path:
        return self.queue_dir / f"{mail_id}.pending"

    def enqueue(
        self,
        mail: Mail,
        error: str = "",
        max_retries: int = 5,
        now: Optional[datetime] = None,
    ) -> PendingSend:
        """Add a failed send to the queue, scheduling the first retry.

        No-op (idempotent) if an entry with the same mail id already exists.
        """
        now = now or datetime.now(timezone.utc)
        with self._lock:
            self._ensure_dir()
            existing = self._path_for(mail.id)
            if existing.exists():
                # Already queued — do not duplicate.
                return PendingSend(**json.loads(existing.read_text()))
            # attempts starts at 1 because the inline send already failed once
            attempts = 1
            pending = PendingSend(
                mail=mail.to_dict(),
                attempts=attempts,
                created_at=now,
                next_attempt=now + timedelta(seconds=_backoff_delay(attempts)),
                last_error=error,
            )
            existing.write_text(json.dumps(pending.model_dump(mode="json"), indent=2))
            return pending

    def list_pending(self) -> list[PendingSend]:
        """All pending sends, oldest first."""
        self._ensure_dir()
        items: list[PendingSend] = []
        for path in sorted(self.queue_dir.glob("*.pending")):
            try:
                items.append(PendingSend(**json.loads(path.read_text())))
            except (json.JSONDecodeError, TypeError):
                continue
        return items

    def load_due(self, now: Optional[datetime] = None) -> list[PendingSend]:
        """Pending sends whose next_attempt has arrived."""
        now = now or datetime.now(timezone.utc)
        return [p for p in self.list_pending() if p.next_attempt <= now]

    def remove(self, mail_id: str) -> bool:
        """Remove a delivered (or exhausted) send from the queue."""
        with self._lock:
            path = self._path_for(mail_id)
            if path.exists():
                path.unlink()
                return True
            return False

    def reschedule(self, pending: PendingSend, error: str, now: Optional[datetime] = None) -> None:
        """Record a failed retry: bump attempt count and push next_attempt out."""
        now = now or datetime.now(timezone.utc)
        with self._lock:
            pending.attempts += 1
            pending.next_attempt = now + timedelta(seconds=_backoff_delay(pending.attempts))
            pending.last_error = error
            self._path_for(pending.mail_id).write_text(
                json.dumps(pending.model_dump(mode="json"), indent=2)
            )

    def purge(self) -> int:
        """Delete all pending files. Returns count removed."""
        self._ensure_dir()
        count = 0
        for path in self.queue_dir.glob("*.pending"):
            path.unlink()
            count += 1
        return count
