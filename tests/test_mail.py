"""Tests for AgentMail Mail object model."""

import hashlib
from datetime import datetime, timezone

from agentmail.mail import Mail, ContentType


class TestMailCreation:
    """Test Mail object creation and field defaults."""

    def test_create_minimal(self):
        mail = Mail(from_addr="alice@host/alice", to_addr="bob@host/bob", message="Hello")
        assert mail.from_addr == "alice@host/alice"
        assert mail.to_addr == "bob@host/bob"
        assert mail.content_type == ContentType.TEXT_PLAIN
        assert mail.message == "Hello"
        assert mail.full_hash  # auto-computed
        assert mail.id  # auto-generated UUID v7

    def test_auto_timestamp(self):
        mail = Mail(from_addr="a@a/a", to_addr="b@b/b", message="hi")
        assert isinstance(mail.at, datetime)
        # Should be roughly now
        delta = (datetime.now(timezone.utc) - mail.at).total_seconds()
        assert abs(delta) < 5

    def test_json_content_type(self):
        mail = Mail(
            from_addr="a@a/a",
            to_addr="b@b/b",
            message='{"task": "done"}',
            content_type=ContentType.APPLICATION_JSON,
        )
        assert mail.content_type == ContentType.APPLICATION_JSON


class TestMailHashing:
    """Test hash computation and verification."""

    def test_hash_auto_computed(self):
        mail = Mail(from_addr="a@a/a", to_addr="b@b/b", message="test")
        assert len(mail.full_hash) == 64  # SHA-256 hex digest

    def test_short_hash(self):
        mail = Mail(from_addr="a@a/a", to_addr="b@b/b", message="test")
        assert len(mail.short_hash) == 16
        assert mail.short_hash == mail.full_hash[:16]

    def test_hash_deterministic(self):
        """Same fields → same hash."""
        kwargs = dict(from_addr="alice@10.0.0.1:8080/alice", to_addr="bob@10.0.0.2:8080/bob", message="ping")
        m1 = Mail(**kwargs)
        m2 = Mail(**kwargs)
        # Different id/at → different hash (they're auto-generated)
        # But if we set same values, hash should match
        m3 = Mail(id=m1.id, at=m1.at, **kwargs)
        assert m3.full_hash == m1.full_hash

    def test_verify_hash_pass(self):
        mail = Mail(from_addr="a@a/a", to_addr="b@b/b", message="verify me")
        assert mail.verify_hash() is True

    def test_verify_hash_tampered(self):
        mail = Mail(from_addr="a@a/a", to_addr="b@b/b", message="original")
        mail.message = "tampered"
        assert mail.verify_hash() is False


class TestMailSerialization:
    """Test to_dict / from_dict roundtrip."""

    def test_roundtrip(self):
        mail = Mail(from_addr="alice@host/alice", to_addr="bob@host/bob", message="Hello")
        d = mail.to_dict()
        assert d["from"] == "alice@host/alice"
        assert d["to"] == "bob@host/bob"
        assert d["content-type"] == "text/plain"
        assert d["full_hash"] == mail.full_hash

    def test_from_dict_roundtrip(self):
        original = Mail(from_addr="alice@host/alice", to_addr="bob@host/bob", message="Round trip")
        d = original.to_dict()
        restored = Mail.from_dict(d)
        assert restored.from_addr == original.from_addr
        assert restored.to_addr == original.to_addr
        assert restored.message == original.message
        assert restored.full_hash == original.full_hash
        assert restored.id == original.id

    def test_from_dict_json_content(self):
        d = {
            "from": "a@a/a",
            "to": "b@b/b",
            "at": "2026-05-13T19:30:00+00:00",
            "id": "01952b3c-4d5e-7f8a-9b0c-1d2e3f4a5b6c",
            "content-type": "application/json",
            "message": '{"status": "ok"}',
            "full_hash": "",
        }
        mail = Mail.from_dict(d)
        assert mail.content_type == ContentType.APPLICATION_JSON