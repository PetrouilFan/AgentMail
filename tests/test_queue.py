"""Tests for the AgentMail retry queue (file-based, exponential backoff)."""

from datetime import datetime, timedelta, timezone

from agentmail.mail import Mail
from agentmail.queue import RetryQueue, PendingSend, _backoff_delay


def _make_mail(from_addr="alice@h/a", to_addr="bob@h/b", message="retry me"):
    return Mail(from_addr=from_addr, to_addr=to_addr, message=message)


class TestBackoffDelay:
    def test_grows_exponentially(self):
        assert _backoff_delay(0) == 2.0
        assert _backoff_delay(1) == 4.0
        assert _backoff_delay(2) == 8.0

    def test_capped(self):
        assert _backoff_delay(20) == 300.0
        assert _backoff_delay(100) == 300.0


class TestRetryQueue:
    def test_enqueue_creates_file(self, tmp_path):
        q = RetryQueue(base_dir=tmp_path)
        mail = _make_mail()
        pending = q.enqueue(mail, error="conn refused")
        assert (tmp_path / "queue" / f"{mail.id}.pending").exists()
        assert pending.attempts == 1
        assert pending.last_error == "conn refused"

    def test_enqueue_idempotent(self, tmp_path):
        q = RetryQueue(base_dir=tmp_path)
        mail = _make_mail()
        first = q.enqueue(mail, error="e1")
        second = q.enqueue(mail, error="e2")
        # Same entry returned, error unchanged (no duplicate)
        assert first.mail_id == second.mail_id
        assert len(q.list_pending()) == 1
        assert second.last_error == "e1"

    def test_list_pending(self, tmp_path):
        q = RetryQueue(base_dir=tmp_path)
        q.enqueue(_make_mail(message="one"))
        q.enqueue(_make_mail(message="two"))
        assert len(q.list_pending()) == 2

    def test_remove(self, tmp_path):
        q = RetryQueue(base_dir=tmp_path)
        mail = _make_mail()
        q.enqueue(mail)
        assert q.remove(mail.id) is True
        assert len(q.list_pending()) == 0
        assert q.remove("nonexistent") is False

    def test_load_due_respects_schedule(self, tmp_path):
        q = RetryQueue(base_dir=tmp_path)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        mail = _make_mail()
        q.enqueue(mail, now=now)
        # Not due yet (next_attempt is now + backoff)
        assert q.load_due(now=now) == []
        # Due after the scheduled delay
        due_time = now + timedelta(seconds=10)
        assert len(q.load_due(now=due_time)) == 1

    def test_reschedule_pushes_next_attempt(self, tmp_path):
        q = RetryQueue(base_dir=tmp_path)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        mail = _make_mail()
        pending = q.enqueue(mail, now=now)
        q.reschedule(pending, error="still failing", now=now)
        assert pending.attempts == 2
        assert pending.last_error == "still failing"
        # Next attempt pushed past `now`
        assert pending.next_attempt > now
        # Persisted
        reloaded = q.list_pending()[0]
        assert reloaded.attempts == 2

    def test_purge(self, tmp_path):
        q = RetryQueue(base_dir=tmp_path)
        q.enqueue(_make_mail(message="a"))
        q.enqueue(_make_mail(message="b"))
        assert q.purge() == 2
        assert q.list_pending() == []
