"""File-based mailbox storage.

Layout:
    ~/.agentmail/
    ├── config.yaml
    ├── keys/
    ├── inbox/       ← active messages (short-hash named)
    ├── outbox/      ← sent messages
    ├── archive/     ← archived by month
    └── logs/
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .mail import Mail
from .config import DEFAULT_CONFIG_DIR


class MailboxStore:
    """File-based mailbox for storing and retrieving Mail objects."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or DEFAULT_CONFIG_DIR
        self.inbox_dir = self.base_dir / "inbox"
        self.outbox_dir = self.base_dir / "outbox"
        self.archive_dir = self.base_dir / "archive"

    def _ensure_dirs(self) -> None:
        for d in (self.inbox_dir, self.outbox_dir, self.archive_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ── Inbox ──────────────────────────────────────────────────────

    def store_inbox(self, mail: Mail) -> Path:
        """Store a received Mail in the inbox. Returns the file path."""
        self._ensure_dirs()
        path = self.inbox_dir / f"{mail.short_hash}.mail"
        path.write_text(json.dumps(mail.to_dict(), indent=2))
        return path

    def list_inbox(self) -> list[dict[str, str]]:
        """List all inbox entries as index rows (from + short_hash)."""
        self._ensure_dirs()
        entries = []
        for path in sorted(self.inbox_dir.glob("*.mail")):
            data = json.loads(path.read_text())
            entries.append({
                "from": data["from"],
                "short_hash": data["full_hash"][:16],
            })
        return entries

    def read_inbox(self, short_hash: str) -> Optional[Mail]:
        """Read a Mail from the inbox by short hash."""
        path = self.inbox_dir / f"{short_hash}.mail"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return Mail.from_dict(data)

    def archive_inbox(self, full_hash: str) -> Optional[Path]:
        """Move a Mail from inbox to archive. Requires the full hash.

        Returns the archive path, or None if not found.
        """
        self._ensure_dirs()
        # Find the file in inbox by checking each mail's full_hash
        for path in self.inbox_dir.glob("*.mail"):
            data = json.loads(path.read_text())
            if data.get("full_hash") == full_hash:
                # Archive by month
                now = datetime.now(timezone.utc)
                month_dir = self.archive_dir / f"{now.year}/{now.month:02d}"
                month_dir.mkdir(parents=True, exist_ok=True)
                dest = month_dir / f"{data['full_hash'][:16]}__{full_hash}.mail"
                shutil.move(str(path), str(dest))
                return dest
        return None

    # ── Outbox ─────────────────────────────────────────────────────

    def store_outbox(self, mail: Mail) -> Path:
        """Store a sent Mail in the outbox. Returns the file path."""
        self._ensure_dirs()
        path = self.outbox_dir / f"{mail.short_hash}.mail"
        path.write_text(json.dumps(mail.to_dict(), indent=2))
        return path

    def list_outbox(self) -> list[dict[str, str]]:
        """List all outbox entries."""
        self._ensure_dirs()
        entries = []
        for path in sorted(self.outbox_dir.glob("*.mail")):
            data = json.loads(path.read_text())
            entries.append({
                "to": data["to"],
                "short_hash": data["full_hash"][:16],
            })
        return entries