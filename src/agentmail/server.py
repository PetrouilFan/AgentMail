"""AgentMail server — FastAPI application with 4 core endpoints.

POST /send    — Send a message to an agent
GET  /inbox  — List received messages (index only)
GET  /read   — Read a full message by short hash
POST /archive — Move a message from inbox to archive
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from contextlib import asynccontextmanager
from fastapi.responses import JSONResponse

from .mail import Mail, ContentType
from .config import AgentMailConfig, load_config, resolve_address, init_config_dir
from .store import MailboxStore
from .transport import HTTPTransport, SendResult

logger = logging.getLogger(__name__)


def create_app(config_path: Optional[Path] = None) -> FastAPI:
    """Create and configure the AgentMail FastAPI application."""
    config = load_config(config_path)
    store = MailboxStore()
    http_transport = HTTPTransport(config)

    # ── Lifespan ────────────────────────────────────────────────────

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_config_dir()
        logger.info(f"AgentMail server started — identity: {config.identity.name}")
        yield
        await http_transport.close()

    app = FastAPI(
        title="AgentMail",
        description="The messaging protocol designed by AI agents, for AI agents.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # ── POST /send ─────────────────────────────────────────────────

    @app.post("/send")
    async def send_mail(
        to: str,
        message: str,
        content_type: str = "text/plain",
        from_addr: Optional[str] = None,
    ):
        """Send a message to an agent.

        The server resolves `to` via the translation table, routes to
        the appropriate transport, and stores a copy in the local outbox.
        Returns mail_hash on success.
        """
        # Resolve sender identity
        sender = from_addr or f"{config.identity.name}@localhost"

        # Resolve recipient
        entry = resolve_address(to, config)
        if not entry and "@" not in to:
            raise HTTPException(
                status_code=404,
                detail=f"Agent '{to}' not found in translation table and not a full address",
            )

        # Validate content type
        try:
            ct = ContentType(content_type)
        except ValueError:
            ct = ContentType.TEXT_PLAIN

        # Create the Mail object
        mail = Mail(
            from_addr=sender,
            to_addr=entry.address if entry else to,
            content_type=ct,
            message=message,
        )

        # Store in local outbox
        store.store_outbox(mail)

        # Send via transport
        result: SendResult = await http_transport.send(to, mail)

        if not result.success:
            logger.warning(f"Send failed for {to}: {result.error}")
            # Still return partial success — message is stored locally

        return {
            "status": "sent" if result.success else "queued",
            "mail_hash": mail.short_hash,
            "full_hash": mail.full_hash,
            "to": mail.to_addr,
            "error": result.error if not result.success else None,
        }

    # ── GET /inbox ──────────────────────────────────────────────────

    @app.get("/inbox")
    async def list_inbox():
        """List received messages. Returns an index, not full contents."""
        entries = store.list_inbox()
        return {"count": len(entries), "messages": entries}

    # ── GET /read ───────────────────────────────────────────────────

    @app.get("/read")
    async def read_mail(hash: str = Query(..., description="Short hash (16 chars)")):
        """Retrieve the full Mail object for a specific message."""
        mail = store.read_inbox(hash)
        if not mail:
            raise HTTPException(status_code=404, detail=f"Message {hash} not found in inbox")
        return mail.to_dict()

    # ── POST /archive ───────────────────────────────────────────────

    @app.post("/archive")
    async def archive_mail(hash: str = Query(..., description="Full hash (64 chars)")):
        """Move a message from inbox to archive.

        Requires the full hash to prevent accidental archiving.
        """
        result = store.archive_inbox(hash)
        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"Message with hash {hash[:16]}... not found in inbox",
            )
        return {"status": "archived", "hash": hash[:16]}

    # ── POST /receive ───────────────────────────────────────────────

    @app.post("/receive")
    async def receive_mail(mail_data: dict):
        """Accept an incoming Mail from a remote agent.

        This is the endpoint that other agents POST to when sending
        messages to this server.
        """
        try:
            mail = Mail.from_dict(mail_data)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid mail data: {e}")

        # Verify integrity
        if not mail.verify_hash():
            raise HTTPException(status_code=400, detail="Hash verification failed")

        # Store in inbox
        store.store_inbox(mail)

        return {"status": "received", "mail_hash": mail.short_hash}

    # ── GET /ping ───────────────────────────────────────────────────

    @app.get("/ping")
    async def ping():
        """Agent metadata endpoint for discovery."""
        return {
            "name": config.identity.name,
            "version": "0.1.0",
            "transports": list(config.transports.keys()) or ["http"],
            "public_key": config.identity.public_key or None,
            "capabilities": [],
        }

    return app


# Default app instance for uvicorn
app = create_app()