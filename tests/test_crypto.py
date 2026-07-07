"""Tests for AgentMail crypto: keyring, signing, verification, E2E encryption."""

import base64
import tempfile
from pathlib import Path

from agentmail.crypto import (
    KeyRing,
    sign_message,
    verify_signature,
    encrypt_body,
    decrypt_body,
)
from agentmail.mail import Mail, ContentType
from agentmail.config import DEFAULT_CONFIG_DIR


def _two_agents():
    """Create two isolated keyrings and have them trust each other."""
    a = KeyRing(Path(tempfile.mkdtemp()))
    a.generate()
    b = KeyRing(Path(tempfile.mkdtemp()))
    b.generate()
    a.add_known_agent("b", b.self_signing_public_pem(), b.self_enc_public_pem())
    b.add_known_agent("a", a.self_signing_public_pem(), a.self_enc_public_pem())
    return a, b


class TestKeyRing:
    def test_generate_creates_files(self):
        kr = KeyRing(Path(tempfile.mkdtemp()))
        kr.generate()
        assert kr.has_identity()
        for f in ("self.key", "self.pub", "self.xkey", "self.xpub"):
            assert (kr.keys_dir / f).exists()

    def test_generate_idempotent_without_overwrite(self):
        kr = KeyRing(Path(tempfile.mkdtemp()))
        kr.generate()
        before = kr.self_signing_public_pem()
        kr.generate()  # no overwrite
        after = kr.self_signing_public_pem()
        assert before == after

    def test_known_agent_storage(self):
        kr = KeyRing(Path(tempfile.mkdtemp()))
        kr.generate()
        other = KeyRing(Path(tempfile.mkdtemp()))
        other.generate()
        kr.add_known_agent("peer", other.self_signing_public_pem(), other.self_enc_public_pem())
        assert kr.known_signing_public("peer") is not None
        assert kr.known_enc_public("peer") is not None
        assert kr.known_signing_public("nobody") is None


class TestSigning:
    def test_sign_and_verify(self):
        a, b = _two_agents()
        m = Mail(from_addr="a@h/a", to_addr="b@h/b", message="hi")
        m.sign(a)
        assert m.signature
        assert m.verify_signature(a)
        assert m.verify_signature(b)  # b trusts a

    def test_unsigned_fails_verify(self):
        a, _ = _two_agents()
        m = Mail(from_addr="a@h/a", to_addr="b@h/b", message="hi")
        assert m.verify_signature(a) is False

    def test_tampered_body_fails_hash(self):
        a, _ = _two_agents()
        m = Mail(from_addr="a@h/a", to_addr="b@h/b", message="hi")
        m.sign(a)
        m.message = "modified"
        assert m.verify_hash() is False

    def test_unknown_sender_falls_back_to_embedded_key(self):
        a, _ = _two_agents()
        m = Mail(from_addr="a@h/a", to_addr="b@h/b", message="hi")
        m.sign(a)
        # A fresh keyring that doesn't know 'a' but the mail carries a.pub
        verifier = KeyRing(Path(tempfile.mkdtemp()))
        verifier.generate()
        assert m.verify_signature(verifier) is True  # embedded key proves authorship


class TestE2EEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        a, b = _two_agents()
        m = Mail(from_addr="a@h/a", to_addr="b@h/b", message="secret", content_type=ContentType.TEXT_PLAIN)
        m.encrypt_for(b.self_enc_public_pem())
        assert m.encrypted
        assert m.message == ""
        # serialize/deserialize
        m2 = Mail.from_dict(m.to_dict())
        assert m2.decrypt(b) == "secret"

    def test_decrypt_with_wrong_key_fails(self):
        a, b = _two_agents()
        eavesdropper = KeyRing(Path(tempfile.mkdtemp()))
        eavesdropper.generate()
        m = Mail(from_addr="a@h/a", to_addr="b@h/b", message="secret")
        m.encrypt_for(b.self_enc_public_pem())
        m2 = Mail.from_dict(m.to_dict())
        try:
            m2.decrypt(eavesdropper)
            assert False, "decrypt should have raised"
        except Exception:
            pass

    def test_sign_then_encrypt_preserves_signature(self):
        a, b = _two_agents()
        m = Mail(from_addr="a@h/a", to_addr="b@h/b", message="secret")
        # Encrypt first (recomputes hash over ciphertext), then sign.
        m.encrypt_for(b.self_enc_public_pem())
        m.sign(a)
        m2 = Mail.from_dict(m.to_dict())
        assert m2.verify_signature(b) is True
        assert m2.decrypt(b) == "secret"
