"""Tests for AgentMail HTTP server endpoints."""

import pytest
from pathlib import Path

from fastapi.testclient import TestClient

from agentmail.server import create_app


@pytest.fixture
def client(tmp_path):
    """Create a test client with an isolated config and mailbox directory."""
    config_file = tmp_path / "config.yaml"
    # Write a minimal config
    config_file.write_text(
        "identity:\n  name: testagent\n  public_key: ''\n"
        "transports: {}\nagents: {}\ndefaults:\n  content_type: text/plain\n"
    )
    app = create_app(config_path=config_file, base_dir=tmp_path)
    return TestClient(app)


class TestSendEndpoint:
    """Test POST /send."""

    def test_send_requires_to_and_message(self, client):
        resp = client.post("/send", params={"to": "someone", "message": "hello"})
        # Will fail to route (no agent in config), but not 422
        assert resp.status_code == 404  # agent not found

    def test_send_with_full_address(self, client):
        """Send to a full address (not in translation table)."""
        resp = client.post(
            "/send",
            params={
                "to": "bob@10.0.0.2:8080/bob",
                "message": "Hello from testagent",
            },
        )
        # The actual HTTP send to bob will fail (no server), but the message
        # should be stored locally and returned as "queued"
        assert resp.status_code == 200
        data = resp.json()
        assert "mail_hash" in data
        assert data["to"] == "bob@10.0.0.2:8080/bob"


class TestReceiveEndpoint:
    """Test POST /receive — accepting incoming mail."""

    def test_receive_valid_mail(self, client):
        from agentmail.mail import Mail

        mail = Mail(from_addr="alice@host/alice", to_addr="testagent@localhost", message="Ping")
        resp = client.post("/receive", json=mail.to_dict())
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "received"
        assert data["mail_hash"] == mail.short_hash

    def test_receive_then_inbox(self, client):
        from agentmail.mail import Mail

        mail = Mail(from_addr="carol@host/carol", to_addr="testagent@localhost", message="Inbox test")
        client.post("/receive", json=mail.to_dict())

        # Check inbox
        resp = client.get("/inbox")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        # Find our message
        hashes = [m["short_hash"] for m in data["messages"]]
        assert mail.short_hash in hashes

    def test_receive_then_read(self, client):
        from agentmail.mail import Mail

        mail = Mail(from_addr="dave@host/dave", to_addr="testagent@localhost", message="Read test")
        client.post("/receive", json=mail.to_dict())

        resp = client.get("/read", params={"hash": mail.short_hash})
        assert resp.status_code == 200
        data = resp.json()
        assert data["from"] == "dave@host/dave"
        assert data["message"] == "Read test"

    def test_receive_then_archive(self, client):
        from agentmail.mail import Mail

        mail = Mail(from_addr="eve@host/eve", to_addr="testagent@localhost", message="Archive test")
        client.post("/receive", json=mail.to_dict())

        resp = client.post("/archive", params={"hash": mail.full_hash})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "archived"

        # Should no longer be in inbox
        resp = client.get("/read", params={"hash": mail.short_hash})
        assert resp.status_code == 404

    def test_receive_invalid_data(self, client):
        resp = client.post("/receive", json={"garbage": True})
        assert resp.status_code == 400


class TestReceiveIdempotency:
    """POST /receive must be idempotent under at-least-once delivery."""

    def test_duplicate_receive_does_not_double_store(self, client):
        from agentmail.mail import Mail

        mail = Mail(from_addr="f@h/f", to_addr="testagent@localhost", message="dup")
        r1 = client.post("/receive", json=mail.to_dict())
        assert r1.status_code == 200
        assert r1.json()["status"] == "received"

        inbox_after_first = client.get("/inbox").json()["count"]

        r2 = client.post("/receive", json=mail.to_dict())
        assert r2.status_code == 200
        assert r2.json()["status"] == "duplicate"

        inbox_after_second = client.get("/inbox").json()["count"]
        assert inbox_after_second == inbox_after_first == 1


class TestContentTypeValidation:
    """POST /send must reject unknown content-types with 400."""

    def test_unknown_content_type_rejected(self, client):
        resp = client.post(
            "/send",
            params={"to": "bob@10.0.0.2:8080/bob", "message": "x", "content_type": "image/png"},
        )
        assert resp.status_code == 400
        assert "Unsupported content-type" in resp.json()["detail"]

    def test_known_content_type_accepted(self, client):
        resp = client.post(
            "/send",
            params={
                "to": "bob@10.0.0.2:8080/bob",
                "message": "json",
                "content_type": "application/json",
            },
        )
        # Stored locally + queued (no live server) → 200
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "queued"


class TestOutboxEndpoint:
    """GET /outbox surfaces sent + pending (retrying) sends."""

    def test_outbox_after_queued_send(self, client):
        client.post("/send", params={"to": "bob@10.0.0.2:8080/bob", "message": "hello"})
        resp = client.get("/outbox")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sent_count"] == 1
        assert data["pending_count"] == 1
        assert data["sent"][0]["to"] == "bob@10.0.0.2:8080/bob"
        assert data["pending"][0]["attempts"] == 1

    def test_outbox_empty(self, client):
        resp = client.get("/outbox")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sent_count"] == 0
        assert data["pending_count"] == 0



class TestReadEndpoint:
    """Test GET /read."""

    def test_read_missing_message(self, client):
        resp = client.get("/read", params={"hash": "0000000000000000"})
        assert resp.status_code == 404


class TestPingEndpoint:
    """Test GET /ping."""

    def test_ping(self, client):
        resp = client.get("/ping")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "testagent"
        assert "version" in data
        assert "transports" in data