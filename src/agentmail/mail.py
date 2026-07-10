"""Mail object definition and serialization.

Every message is a Mail — a self-contained envelope with cryptographic identity.

The ``full_hash`` is computed over the core envelope (from/to/at/id/content-type/
message) and serves as the canonical permanent identifier. The signature and
encryption fields are NOT part of the hash input, so signing and hashing stay
independent and stable across hops.

Cryptographic fields:
    signature     Ed25519 signature over full_hash (sender's identity key)
    public_key    base64 Ed25519 public key of the sender (for verification)
    encrypted     bool — True if the body is E2E-encrypted
    ciphertext    base64 ChaCha20-Poly1305 ciphertext (when encrypted)
    nonce         base64 nonce (when encrypted)
    ephemeral_key base64 X25519 ephemeral public key (when encrypted)

When ``encrypted`` is True the cleartext ``message`` is empty and the body lives
in ``ciphertext``. The inbox still shows ``from`` and ``full_hash`` cleartext for
routing/triage; only the body is concealed.
"""

from __future__ import annotations

import hashlib
import base64
import json
import uuid
import uuid6
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, cast

from pydantic import BaseModel, Field, field_validator

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from .crypto import KeyRing, sign_message, verify_signature, encrypt_body, decrypt_body


class ContentType(str, Enum):
    """Supported content types for Mail bodies."""

    TEXT_PLAIN = "text/plain"
    APPLICATION_JSON = "application/json"
    APPLICATION_OCTET_STREAM = "application/octet-stream"
    MULTIPART_MIXED = "multipart/mixed"
    APPLICATION_X_TASK_REQUEST = "application/x-task-request"
    APPLICATION_X_TASK_RESULT = "application/x-task-result"


class MailPart(BaseModel):
    """A single part of a multipart/mixed message (a file or binary blob).

    ``content`` is the raw bytes; it is base64-encoded on the wire (in
    ``Mail.parts_b64``) so the JSON envelope stays valid. ``filename`` is
    optional but recommended for round-tripping attachments.
    """

    filename: str = ""
    content_type: str = "application/octet-stream"
    content: bytes = b""

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "content_type": self.content_type,
            "content_b64": base64.b64encode(self.content).decode("ascii"),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MailPart":
        return cls(
            filename=d.get("filename", ""),
            content_type=d.get("content_type", "application/octet-stream"),
            content=base64.b64decode(d.get("content_b64", "")),
        )


class Mail(BaseModel):
    """A single Mail message — the core data structure of AgentMail."""

    from_addr: str = Field(..., description="Full address of the sender")
    to_addr: str = Field(..., description="Full address of the recipient")
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    id: str = Field(default_factory=lambda: str(uuid6.uuid7()))
    content_type: ContentType = Field(default=ContentType.TEXT_PLAIN)
    message: str = Field(default="", description="The payload body (cleartext unless encrypted)")
    # Multipart attachments (used when content_type == multipart/mixed).
    parts: list[MailPart] = Field(default_factory=list, description="Binary/file parts for multipart messages")
    full_hash: str = Field(default="", description="SHA-256 of the core envelope (auto-computed)")

    # ── Cryptographic footer (Ed25519 identity) ──
    signature: str = Field(default="", description="Ed25519 signature over full_hash")
    public_key: str = Field(default="", description="base64 Ed25519 public key of sender")

    # ── End-to-end encryption (X25519 + ChaCha20-Poly1305) ──
    encrypted: bool = Field(default=False, description="True if body is E2E-encrypted")
    ciphertext: str = Field(default="", description="base64 AEAD ciphertext (when encrypted)")
    nonce: str = Field(default="", description="base64 nonce (when encrypted)")
    ephemeral_key: str = Field(default="", description="base64 X25519 ephemeral pubkey (when encrypted)")

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
        """Compute SHA-256 digest over the core envelope (crypto fields excluded).

        The body used for hashing is the cleartext ``message`` when not encrypted,
        or the ``ciphertext`` when E2E-encrypted — i.e. whatever actually travels
        on the wire. This keeps the hash stable across sign → encrypt → transfer.
        """
        body = self.ciphertext if self.encrypted else self.message
        payload = (
            f"{self.from_addr}\n"
            f"{self.to_addr}\n"
            f"{self.at.isoformat()}\n"
            f"{self.id}\n"
            f"{self.content_type.value}\n"
            f"{body}\n"
            f"{''.join(p.to_dict()['content_b64'] for p in self.parts) if self.parts else ''}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def verify_hash(self) -> bool:
        """Re-compute hash and compare — integrity check."""
        return self._compute_hash() == self.full_hash

    # ── Signing / verification ─────────────────────────────────

    def sign(self, keyring: KeyRing) -> None:
        """Sign this Mail with the local identity key.

        Computes the signature over full_hash and stamps the sender's public
        key into the envelope. Idempotent — re-signing overwrites.
        """
        if not self.full_hash:
            self.full_hash = self._compute_hash()
        self.signature = sign_message(keyring.signing_private(), self.full_hash)
        self.public_key = base64.b64encode(keyring.self_signing_public_pem()).decode("ascii")

    def verify_signature(self, keyring: KeyRing) -> bool:
        """Verify the Ed25519 signature against a trusted known-agent key.

        The sender's agent name is derived from the ``from`` address
        (device_name@.../agent_name). Returns False if unsigned or unknown.
        """
        if not self.signature:
            return False
        # Prefer a locally-trusted known-agent key by sender name.
        sender_name = self.from_addr.split("/")[-1] if "/" in self.from_addr else self.from_addr
        known = keyring.known_signing_public(sender_name)
        if known is not None:
            return verify_signature(known, self.full_hash, self.signature)
        # Fall back to the embedded public key (still proves the signer holds
        # the private key matching the key they advertised).
        if not self.public_key:
            return False
        try:
            pub = cast(
                ed25519.Ed25519PublicKey,
                serialization.load_pem_public_key(base64.b64decode(self.public_key)),
            )
        except Exception:
            return False
        return verify_signature(pub, self.full_hash, self.signature)

    # ── E2E encryption ─────────────────────────────────────────

    def encrypt_for(self, recipient_enc_public_pem: bytes) -> None:
        """Encrypt the body for a recipient (X25519 + ChaCha20-Poly1305).

        Replaces ``message`` with the ciphertext fields and sets ``encrypted``.
        The recipient's X25519 public key is supplied as PEM bytes.
        """
        recipient_pub = cast(
            x25519.X25519PublicKey,
            serialization.load_pem_public_key(recipient_enc_public_pem),
        )
        ephemeral = x25519.X25519PrivateKey.generate()
        ct, nonce, epk = encrypt_body(recipient_pub, ephemeral, self.message.encode("utf-8"))
        self.ciphertext = ct
        self.nonce = nonce
        self.ephemeral_key = epk
        self.encrypted = True
        self.message = ""  # cleartext no longer carried in the envelope
        # Recompute the hash over the ciphertext (the body that travels), so
        # the signature (applied after encrypt) covers the wire representation.
        self.full_hash = self._compute_hash()
        self.signature = ""  # must be re-signed over the new hash

    def decrypt(self, keyring: KeyRing) -> str:
        """Decrypt the body using the local identity encryption key.

        Returns the cleartext message. Raises ValueError if not encrypted or
        decryption fails.
        """
        if not self.encrypted:
            return self.message
        plaintext = decrypt_body(
            self.ephemeral_key, self.nonce, self.ciphertext, keyring.enc_private()
        )
        return plaintext.decode("utf-8")

    # ── Serialization ──────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a flat dict suitable for JSON storage or API responses."""
        return {
            "from": self.from_addr,
            "to": self.to_addr,
            "at": self.at.isoformat(),
            "id": self.id,
            "content-type": self.content_type.value,
            "message": self.message,
            "parts": [p.to_dict() for p in self.parts],
            "full_hash": self.full_hash,
            "signature": self.signature,
            "public_key": self.public_key,
            "encrypted": str(self.encrypted),
            "ciphertext": self.ciphertext,
            "nonce": self.nonce,
            "ephemeral_key": self.ephemeral_key,
        }

    # ── Binary / multipart convenience helpers ───────────────────

    def add_binary_part(
        self,
        data: bytes,
        filename: str = "",
        content_type: str = "application/octet-stream",
    ) -> "Mail":
        """Attach a binary blob as a multipart/mixed part.

        Sets content_type to multipart/mixed if not already, so the receiving
        side knows to interpret ``parts``. Returns self for chaining. Recomputes
        full_hash so the envelope stays self-consistent.
        """
        self.parts.append(
            MailPart(filename=filename, content_type=content_type, content=data)
        )
        if self.content_type not in (ContentType.MULTIPART_MIXED,):
            self.content_type = ContentType.MULTIPART_MIXED
        self.full_hash = self._compute_hash()
        return self

    def decode_binary(self) -> list[tuple[str, str, bytes]]:
        """Return [(filename, content_type, content)] for multipart messages."""
        return [(p.filename, p.content_type, p.content) for p in self.parts]

    # ── Structured task-mail (protocol-level wire contract) ──────

    @classmethod
    def make_task_request(
        cls,
        from_addr: str,
        to_addr: str,
        task_id: str,
        payload: dict,
        reply_to: str | None = None,
        ttl: int | None = None,
    ) -> "Mail":
        """Build an application/x-task-request Mail (protocol wire contract)."""
        body = {
            "task_id": task_id,
            "reply_to": reply_to or from_addr,
            "payload": payload,
        }
        if ttl is not None:
            body["ttl"] = ttl
        return cls(
            from_addr=from_addr,
            to_addr=to_addr,
            content_type=ContentType.APPLICATION_X_TASK_REQUEST,
            message=json.dumps(body, separators=(",", ":")),
        )

    @classmethod
    def make_task_result(
        cls,
        from_addr: str,
        to_addr: str,
        task_id: str,
        status: str,
        result: Any = None,
        error: str | None = None,
        agent: str | None = None,
    ) -> "Mail":
        """Build an application/x-task-result Mail (protocol wire contract)."""
        body = {"task_id": task_id, "status": status}
        if result is not None:
            body["result"] = result
        if error is not None:
            body["error"] = error
        if agent is not None:
            body["agent"] = agent
        return cls(
            from_addr=from_addr,
            to_addr=to_addr,
            content_type=ContentType.APPLICATION_X_TASK_RESULT,
            message=json.dumps(body, separators=(",", ":")),
        )

    def parse_task(self) -> dict:
        """Parse a task-request/task-result body into a dict.

        Raises ValueError if the content-type is not a task type or if the
        JSON is invalid. This is the only contract consumer code should rely
        on — it keeps the schema enforcement in one place (protocol-general).
        """
        if self.content_type not in (
            ContentType.APPLICATION_X_TASK_REQUEST,
            ContentType.APPLICATION_X_TASK_RESULT,
        ):
            raise ValueError(f"not a task message: {self.content_type}")
        return json.loads(self.message)

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "Mail":
        """Deserialize from a flat dict (API response or stored Mail)."""
        return cls(
            from_addr=data["from"],
            to_addr=data["to"],
            at=data["at"],
            id=data["id"],
            content_type=ContentType(data.get("content-type", "text/plain")),
            message=data.get("message", ""),
            parts=[MailPart.from_dict(p) for p in data.get("parts", [])],
            full_hash=data.get("full_hash", ""),
            signature=data.get("signature", ""),
            public_key=data.get("public_key", ""),
            encrypted=data.get("encrypted", "False") in ("True", "true", "1"),
            ciphertext=data.get("ciphertext", ""),
            nonce=data.get("nonce", ""),
            ephemeral_key=data.get("ephemeral_key", ""),
        )
