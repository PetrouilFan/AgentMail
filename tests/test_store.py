"""Tests for AgentMail file-based mailbox storage."""

import json
from pathlib import Path

from agentmail.mail import Mail, ContentType
from agentmail.store import MailboxStore


def _make_mail(from_addr="alice@10.0.0.1:8080/alice", to_addr="bob@10.0.0.2:8080/bob", message="Hello"):
    return Mail(from_addr=from_addr, to_addr=to_addr, message=message)


class TestInboxStorage:
    """Test inbox store, list, and read operations."""

    def test_store_and_list(self, tmp_path):
        store = MailboxStore(base_dir=tmp_path)
        mail = _make_mail()
        store.store_inbox(mail)

        entries = store.list_inbox()
        assert len(entries) == 1
        assert entries[0]["from"] == mail.from_addr
        assert entries[0]["short_hash"] == mail.short_hash

    def test_read_by_short_hash(self, tmp_path):
        store = MailboxStore(base_dir=tmp_path)
        original = _make_mail(message="important data")
        store.store_inbox(original)

        read_back = store.read_inbox(original.short_hash)
        assert read_back is not None
        assert read_back.from_addr == original.from_addr
        assert read_back.message == original.message
        assert read_back.full_hash == original.full_hash

    def test_read_missing_returns_none(self, tmp_path):
        store = MailboxStore(base_dir=tmp_path)
        result = store.read_inbox("deadbeef12345678")
        assert result is None

    def test_multiple_mails(self, tmp_path):
        store = MailboxStore(base_dir=tmp_path)
        m1 = _make_mail(message="first")
        m2 = _make_mail(message="second")
        store.store_inbox(m1)
        store.store_inbox(m2)

        entries = store.list_inbox()
        assert len(entries) == 2


class TestArchive:
    """Test archive operations (inbox → archive)."""

    def test_archive_by_full_hash(self, tmp_path):
        store = MailboxStore(base_dir=tmp_path)
        mail = _make_mail(message="archive me")
        store.store_inbox(mail)

        result_path = store.archive_inbox(mail.full_hash)
        assert result_path is not None
        assert result_path.exists()

        # Should no longer appear in inbox
        assert store.read_inbox(mail.short_hash) is None

    def test_archive_wrong_hash_returns_none(self, tmp_path):
        store = MailboxStore(base_dir=tmp_path)
        mail = _make_mail()
        store.store_inbox(mail)

        result = store.archive_inbox("0" * 64)  # wrong hash
        assert result is None

    def test_archive_preserves_content(self, tmp_path):
        store = MailboxStore(base_dir=tmp_path)
        mail = _make_mail(message="preserve this")
        store.store_inbox(mail)

        archive_path = store.archive_inbox(mail.full_hash)
        assert archive_path is not None
        data = json.loads(archive_path.read_text())
        assert data["message"] == "preserve this"
        assert data["full_hash"] == mail.full_hash


class TestOutbox:
    """Test outbox storage."""

    def test_store_and_list_outbox(self, tmp_path):
        store = MailboxStore(base_dir=tmp_path)
        mail = _make_mail(message="sent item")
        store.store_outbox(mail)

        entries = store.list_outbox()
        assert len(entries) == 1
        assert entries[0]["to"] == mail.to_addr
        assert entries[0]["short_hash"] == mail.short_hash