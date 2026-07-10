"""AgentMail server — FastAPI application with the 4 core endpoints.

POST /send    — Send a message to an agent
GET  /inbox  — List received messages (index only)
GET  /read   — Read a full message by short hash
POST /archive — Move a message from inbox to archive
POST /receive — Accept incoming Mail from a remote agent
GET  /outbox — List locally queued (pending/sent) messages
GET  /ping   — Agent metadata for discovery
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from contextlib import asynccontextmanager

from cryptography.hazmat.primitives import serialization

from .mail import Mail, ContentType
from .config import AgentMailConfig, load_config, resolve_address, init_config_dir, TransportConfig
from .store import MailboxStore
from .transport import HTTPTransport, SendResult
from .queue import RetryQueue
from .crypto import KeyRing

logger = logging.getLogger(__name__)

# How often the background retry worker wakes to check for due sends.
RETRY_POLL_INTERVAL = 5.0


def create_app(config_path: Optional[Path] = None, base_dir: Optional[Path] = None) -> FastAPI:
    """Create and configure the AgentMail FastAPI application.

    Args:
        config_path: Explicit config.yaml path. Defaults to ~/.agentmail/config.yaml.
        base_dir: Mailbox/queue root. Defaults to ~/.agentmail. Tests pass a
            temporary directory here to isolate filesystem state.
    """
    config = load_config(config_path)
    store = MailboxStore(base_dir=base_dir)
    http_conf = config.transports.get("http")
    tls = bool(http_conf and http_conf.tls)
    http_transport = HTTPTransport(config, tls=tls)
    retry_queue = RetryQueue(base_dir=base_dir)
    keyring = KeyRing(base_dir=base_dir)
    keyring.ensure()

    async def _attempt_send(to: str, mail: Mail) -> SendResult:
        """Try one HTTP delivery; enqueue on failure."""
        result = await http_transport.send(mail.to_addr, mail)
        if not result.success:
            max_retries = config.defaults.max_retries
            retry_queue.enqueue(mail, error=result.error or "delivery failed", max_retries=max_retries)
            logger.warning(f"Send failed for {to}: {result.error} — queued for retry")
        return result

    def _relay_federation(mail: Mail) -> bool:
        """Relay an unresolvable send to known federation routers.

        Returns True if at least one relay accepted the mail. Federation is
        opt-in (defaults.federation); a node relays only when enabled. Each
        router re-evaluates — if it still can't resolve, it relays onward to
        ITS routers (loop bounded by the mail id + per-hop dedup at /receive).
        """
        if not config.defaults.federation:
            return False
        # Routers = peers flagged router:true in their transport, or any peer
        # if no explicit routers are configured (flood-bounded by idempotency).
        routers = [
            e for e in config.agents.values()
            if (config.transports.get(e.transport) or TransportConfig()).router
        ] or list(config.agents.values())
        relayed = False
        for entry in routers:
            try:
                scheme = "https" if entry.transport == "https" else "http"
                # Use the /receive endpoint so the router ingests it as an
                # incoming mail (and re-resolves + relays/sends onward).
                url = f"{scheme}://{entry.address.split('@')[-1].split('/')[0]}/receive"
                with httpx.Client(timeout=2.0) as c:
                    r = c.post(url, json=mail.to_dict())
                if r.status_code == 200:
                    relayed = True
                    logger.info(f"Federated {mail.short_hash} via router {entry.address}")
            except Exception as e:
                logger.warning(f"Federation relay to {entry.address} failed: {e}")
        return relayed

    async def _retry_worker() -> None:
        """Background loop: retry any due pending sends with backoff."""
        while True:
            await asyncio.sleep(RETRY_POLL_INTERVAL)
            try:
                due = retry_queue.load_due()
                # Reload config so max_retries reflects live (post-trust) settings.
                cfg = load_config(app.extra.get("config_path"))
                for pending in due:
                    mail = Mail.from_dict(pending.mail)
                    # Skip if we've exhausted retries
                    if pending.attempts > cfg.defaults.max_retries:
                        retry_queue.remove(pending.mail_id)
                        continue
                    result = await http_transport.send(pending.mail_id, mail)
                    if result.success:
                        retry_queue.remove(pending.mail_id)
                        logger.info(f"Retry delivered {mail.short_hash} to {mail.to_addr}")
                    else:
                        retry_queue.reschedule(pending, error=result.error or "retry failed")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let the worker die
                logger.exception(f"Retry worker error: {e}")

    # ── Lifespan ────────────────────────────────────────────────────

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_config_dir(base_dir)
        # Re-enqueue any pending sends that survived a restart.
        pending = retry_queue.list_pending()
        if pending:
            logger.info(f"AgentMail started — {len(pending)} pending send(s) in retry queue")
        else:
            logger.info(f"AgentMail server started — identity: {config.identity.name}")
        worker = asyncio.create_task(_retry_worker())
        yield
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await http_transport.close()

    app = FastAPI(
        title="AgentMail",
        description="The messaging protocol designed by AI agents, for AI agents.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.extra["base_dir"] = base_dir
    app.extra["config_path"] = config_path

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
        Returns mail_hash on success. On transport failure the message is
        queued for retry with exponential backoff.
        """
        # Re-read config per request so trust/bootstrap updates to the
        # translation table take effect without a server restart.
        config_path = app.extra.get("config_path")
        config = load_config(config_path)
        # Resolve sender identity — default to a routable address so recipients
        # can reply (name@host:port/name) rather than an unreachable @localhost.
        if from_addr:
            sender = from_addr
        else:
            host = (http_conf.host if http_conf and http_conf.host not in ("0.0.0.0",) else "localhost")
            port = (http_conf.port if http_conf else 12345)
            sender = f"{config.identity.name}@{host}:{port}/{config.identity.name}"

        # Validate content type strictly — reject unknown types instead of
        # silently downgrading to text/plain. Done before resolution so the
        # federation relay path below can reuse `ct`.
        try:
            ct = ContentType(content_type)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported content-type '{content_type}'. "
                f"Supported: {[c.value for c in ContentType]}",
            )

        # Resolve recipient
        entry = resolve_address(to, config)
        if not entry and "@" not in to:
            # Not in our translation table. If federation is enabled, attempt
            # to relay through a known router before giving up.
            if config.defaults.federation:
                relay_to = to if "@" in to else to
                relay_mail = Mail(
                    from_addr=sender,
                    to_addr=(to if "@" in to else to),
                    content_type=ct,
                    message=message,
                )
                if keyring.has_identity():
                    rcpt_name = to.split("/")[-1] if "@" in to else to
                    rcpt_enc = keyring.known_enc_public(rcpt_name)
                    if rcpt_enc is not None:
                        relay_mail.encrypt_for(rcpt_enc.public_bytes(
                            encoding=serialization.Encoding.PEM,
                            format=serialization.PublicFormat.SubjectPublicKeyInfo,
                        ))
                    relay_mail.sign(keyring)
                if _relay_federation(relay_mail):
                    return {
                        "status": "federated",
                        "mail_hash": relay_mail.short_hash,
                        "full_hash": relay_mail.full_hash,
                        "to": relay_mail.to_addr,
                        "error": None,
                    }
            raise HTTPException(
                status_code=404,
                detail=f"Agent '{to}' not found in translation table and not a full address",
            )

        # Create the Mail object
        mail = Mail(
            from_addr=sender,
            to_addr=entry.address if entry else to,
            content_type=ct,
            message=message,
        )

        # Sign with our identity key (if we have one). Encrypt-then-sign so the
        # signature covers the ciphertext that actually travels on the wire.
        if keyring.has_identity():
            # End-to-end encrypt the body if we know the recipient's enc key.
            recipient_name = (entry.address if entry else to).split("/")[-1]
            recipient_enc = keyring.known_enc_public(recipient_name)
            if recipient_enc is not None:
                recipient_pem = recipient_enc.public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                mail.encrypt_for(recipient_pem)
            mail.sign(keyring)

        # Store in local outbox (delivery confirmation)
        store.store_outbox(mail)

        # Attempt delivery
        result = await _attempt_send(to, mail)

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
        """Retrieve the full Mail object for a specific message.

        Auto-archives the message after reading (mailbox-not-chat: read-once
        semantics). Idempotent — if it's already been read/archived, this is a
        no-op and the mail is still returned.
        """
        mail = store.read_inbox(hash)
        if not mail:
            raise HTTPException(status_code=404, detail=f"Message {hash} not found in inbox")
        # Auto-archive after read so the inbox only holds unread mail.
        try:
            store.archive_inbox(mail.full_hash)
        except Exception:
            logger.warning(f"Auto-archive failed for {mail.short_hash}")
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
        messages to this server. Delivery is at-least-once, so idempotency
        is enforced here by the Mail's UUID — a duplicate is acknowledged
        without re-storing.
        """
        try:
            mail = Mail.from_dict(mail_data)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid mail data: {e}")

        # Verify integrity
        if not mail.verify_hash():
            raise HTTPException(status_code=400, detail="Hash verification failed")

        # Verify the sender's signature (if required by policy).
        if config.defaults.require_signature and not mail.verify_signature(keyring):
            raise HTTPException(status_code=403, detail="Signature verification failed")

        # Idempotency: drop duplicates (same id) so at-least-once transport
        # semantics don't create duplicate inbox entries.
        if store.inbox_has_id(mail.id):
            return {"status": "duplicate", "mail_hash": mail.short_hash}

        # Decrypt the body locally so the inbox copy is readable by this agent.
        if mail.encrypted and keyring.has_identity():
            try:
                mail.message = mail.decrypt(keyring)
                mail.encrypted = False
                mail.ciphertext = mail.nonce = mail.ephemeral_key = ""
                # Recompute hash over the now-cleartext body so the stored copy
                # remains self-consistent (verify_hash passes on /read).
                mail.full_hash = mail._compute_hash()
            except Exception as e:
                logger.warning(f"Failed to decrypt incoming mail {mail.short_hash}: {e}")

        # Store in inbox
        store.store_inbox(mail)

        return {"status": "received", "mail_hash": mail.short_hash}

    # ── GET /outbox ─────────────────────────────────────────────────

    @app.get("/outbox")
    async def list_outbox():
        """List locally stored outbox messages and pending (retrying) sends."""
        sent = store.list_outbox()
        pending = [
            {
                "mail_id": p.mail_id,
                "short_hash": p.full_hash[:16],
                "to": p.mail.get("to", ""),
                "attempts": p.attempts,
                "next_attempt": p.next_attempt.isoformat(),
                "last_error": p.last_error,
            }
            for p in retry_queue.list_pending()
        ]
        return {
            "sent_count": len(sent),
            "pending_count": len(pending),
            "sent": sent,
            "pending": pending,
        }

    # ── GET /ping ───────────────────────────────────────────────────

    @app.get("/ping")
    async def ping():
        """Agent metadata endpoint for discovery and trust bootstrap.

        Returns the real keyring public keys (signing + encryption) so peers
        can establish trust via `agentmail trust <url>` without manual key
        file exchange.
        """
        own_addr = None
        if http_conf:
            host = http_conf.host if http_conf.host not in ("0.0.0.0",) else "localhost"
            own_addr = f"{config.identity.name}@{host}:{http_conf.port}/{config.identity.name}"
        signing_pub = keyring.self_signing_public_pem().decode("ascii") if keyring.has_identity() else None
        enc_pub = keyring.self_enc_public_pem().decode("ascii") if keyring.has_identity() else None
        return {
            "name": config.identity.name,
            "version": "0.1.0",
            "transports": list(config.transports.keys()) or ["http"],
            "tls": bool(http_conf and http_conf.tls),
            "address": own_addr,
            "public_key": signing_pub,
            "encryption_key": enc_pub,
            "capabilities": [],
        }

    return app


# Default app instance for uvicorn
app = create_app()
