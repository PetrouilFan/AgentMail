"""AgentMail cryptographic identity — keyring, signing, and E2E encryption.

Identity keys (Ed25519) sign every Mail so recipients can verify authorship.
Encryption keys (X25519) provide per-message envelope encryption (E2E) of the
body via ChaCha20-Poly1305 AEAD.

Keyring layout (under config_dir/keys):
    self.key      Ed25519 private key (this agent's signing identity)
    self.pub      Ed25519 public key
    self.xkey     X25519 private key (for E2E decryption)
    self.xpub     X25519 public key
    known_agents/
        <name>.pub       Ed25519 public key of a trusted agent
        <name>.xpub      X25519 public key of a trusted agent (optional)

Trust is local and explicit — you add keys for agents you communicate with.
No global registry, no PKI.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Optional, cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from .config import DEFAULT_CONFIG_DIR


# ── Key material helpers (PEM / raw bytes) ──────────────────────────

def _b64_encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64_decode(s: str) -> bytes:
    return base64.b64decode(s)


def _load_private(path: Path, klass):
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def _load_public(path: Path, klass):
    return serialization.load_pem_public_key(path.read_bytes())


class KeyRing:
    """Local cryptographic identity: signing (Ed25519) + encryption (X25519)."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or DEFAULT_CONFIG_DIR
        self.keys_dir = self.base_dir / "keys"
        self.known_dir = self.keys_dir / "known_agents"

    # ── Directory bootstrap ──────────────────────────────────────

    def ensure(self) -> None:
        """Create the keyring directory structure if missing."""
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        self.known_dir.mkdir(parents=True, exist_ok=True)

    # ── Key generation ───────────────────────────────────────────

    def generate(self, overwrite: bool = False) -> None:
        """Generate a fresh identity (Ed25519 + X25519) if absent or overwrite."""
        self.ensure()
        if self.has_identity() and not overwrite:
            return
        signing = ed25519.Ed25519PrivateKey.generate()
        enc = x25519.X25519PrivateKey.generate()
        self._write_private(signing, self.keys_dir / "self.key")
        self._write_public(signing.public_key(), self.keys_dir / "self.pub")
        self._write_private(enc, self.keys_dir / "self.xkey")
        self._write_public(enc.public_key(), self.keys_dir / "self.xpub")

    # ── Writers ──────────────────────────────────────────────────

    @staticmethod
    def _write_private(key, path: Path) -> None:
        path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

    @staticmethod
    def _write_public(key, path: Path) -> None:
        path.write_bytes(
            key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

    # ── Presence checks ──────────────────────────────────────────

    def has_identity(self) -> bool:
        return (self.keys_dir / "self.key").exists() and (self.keys_dir / "self.xkey").exists()

    # ── Loaders ──────────────────────────────────────────────────

    def signing_private(self) -> ed25519.Ed25519PrivateKey:
        return cast(ed25519.Ed25519PrivateKey, _load_private(self.keys_dir / "self.key", ed25519.Ed25519PrivateKey))

    def signing_public(self) -> ed25519.Ed25519PublicKey:
        return cast(ed25519.Ed25519PublicKey, _load_public(self.keys_dir / "self.pub", ed25519.Ed25519PublicKey))

    def enc_private(self) -> x25519.X25519PrivateKey:
        return cast(x25519.X25519PrivateKey, _load_private(self.keys_dir / "self.xkey", x25519.X25519PrivateKey))

    def enc_public(self) -> x25519.X25519PublicKey:
        return cast(x25519.X25519PublicKey, _load_public(self.keys_dir / "self.xpub", x25519.X25519PublicKey))

    # ── Known agents ─────────────────────────────────────────────

    def add_known_agent(self, name: str, ed25519_pub_pem: bytes, x25519_pub_pem: Optional[bytes] = None) -> None:
        """Trust an agent by storing its public key(s)."""
        self.ensure()
        (self.known_dir / f"{name}.pub").write_bytes(ed25519_pub_pem)
        if x25519_pub_pem is not None:
            (self.known_dir / f"{name}.xpub").write_bytes(x25519_pub_pem)

    def known_signing_public(self, name: str) -> Optional[ed25519.Ed25519PublicKey]:
        path = self.known_dir / f"{name}.pub"
        if not path.exists():
            return None
        return cast(ed25519.Ed25519PublicKey, _load_public(path, ed25519.Ed25519PublicKey))

    def known_enc_public(self, name: str) -> Optional[x25519.X25519PublicKey]:
        path = self.known_dir / f"{name}.xpub"
        if not path.exists():
            return None
        return cast(x25519.X25519PublicKey, _load_public(path, x25519.X25519PublicKey))

    # ── PEM exporters (for keyring exchange / config) ────────────

    def self_signing_public_pem(self) -> bytes:
        return self.signing_public().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def self_enc_public_pem(self) -> bytes:
        return self.enc_public().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )


# ── Signing ──────────────────────────────────────────────────────

def sign_message(private_key: ed25519.Ed25519PrivateKey, full_hash: str) -> str:
    """Ed25519 signature over the mail's full_hash, base64-encoded."""
    sig = private_key.sign(full_hash.encode("utf-8"))
    return _b64_encode(sig)


def verify_signature(public_key: ed25519.Ed25519PublicKey, full_hash: str, signature_b64: str) -> bool:
    """Verify an Ed25519 signature (base64) over full_hash. Returns bool."""
    try:
        public_key.verify(_b64_decode(signature_b64), full_hash.encode("utf-8"))
        return True
    except Exception:
        return False


# ── Envelope encryption (X25519 + ChaCha20-Poly1305) ──────────────

def encrypt_body(
    recipient_enc_public: x25519.X25519PublicKey,
    sender_enc_private: x25519.X25519PrivateKey,
    plaintext: bytes,
) -> tuple[str, str, str]:
    """Encrypt a message body for a recipient.

    Returns (ciphertext_b64, nonce_b64, ephemeral_pubkey_b64) so the
    recipient can derive the same shared secret without prior key exchange.
    """
    ephemeral = x25519.X25519PrivateKey.generate()
    shared = ephemeral.exchange(recipient_enc_public)
    nonce = os.urandom(12)  # ChaCha20-Poly1305 nonce size
    aead = ChaCha20Poly1305(shared)
    ct = aead.encrypt(nonce, plaintext, None)
    return _b64_encode(ct), _b64_encode(nonce), _b64_encode(
        ephemeral.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def decrypt_body(
    ephemeral_pub_b64: str,
    nonce_b64: str,
    ciphertext_b64: str,
    recipient_enc_private: x25519.X25519PrivateKey,
) -> bytes:
    """Decrypt a body using our X25519 private key + the sender's ephemeral pubkey."""
    ephemeral_pub = x25519.X25519PublicKey.from_public_bytes(_b64_decode(ephemeral_pub_b64))
    shared = recipient_enc_private.exchange(ephemeral_pub)
    aead = ChaCha20Poly1305(shared)
    return aead.decrypt(_b64_decode(nonce_b64), _b64_decode(ciphertext_b64), None)
