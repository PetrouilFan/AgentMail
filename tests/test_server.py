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

    def test_read_auto_archives(self, client):
        """Reading a message must remove it from the inbox (read-once)."""
        from agentmail.mail import Mail, ContentType

        m = Mail(
            from_addr="peer@h/peer",
            to_addr="testagent@localhost",
            content_type=ContentType.TEXT_PLAIN,
            message="auto-archive me",
        )
        rcv = client.post("/receive", json=m.to_dict())
        assert rcv.status_code == 200
        inbox = client.get("/inbox").json()
        assert inbox["count"] == 1
        short = inbox["messages"][0]["short_hash"]

        read = client.get("/read", params={"hash": short})
        assert read.status_code == 200
        assert read.json()["message"] == "auto-archive me"

        # After reading, the inbox must be empty (auto-archived).
        assert client.get("/inbox").json()["count"] == 0

        # Re-reading the same short hash now 404s (it's no longer in the inbox).
        assert client.get("/read", params={"hash": short}).status_code == 404


class TestPingEndpoint:
    """Test GET /ping."""

    def test_ping(self, client):
        resp = client.get("/ping")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "testagent"
        assert "version" in data
        assert "transports" in data


@pytest.fixture
def secured_client(tmp_path):
    """Client with a real keyring, a known agent 'peer', and require_signature on."""
    from agentmail.crypto import KeyRing

    base = Path(tmp_path / "data")
    kr = KeyRing(base_dir=base)
    kr.generate()
    # Known agent 'peer' (we act as both ends in the test)
    peer = KeyRing(Path(tmp_path / "peer"))
    peer.generate()
    kr.add_known_agent("peer", peer.self_signing_public_pem(), peer.self_enc_public_pem())

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "identity:\n  name: testagent\n  public_key: ''\n"
        "transports: {}\nagents: {}\n"
        "defaults:\n  content_type: text/plain\n  require_signature: true\n"
    )
    app = create_app(config_path=config_file, base_dir=base)
    app.extra["peer_keyring"] = peer
    return TestClient(app)


class TestSecurityEndpoints:
    """Signing + encryption flow through the server."""

    def test_send_signs_when_identity_exists(self, secured_client):
        from agentmail.mail import Mail
        from agentmail.crypto import KeyRing
        import json
        from pathlib import Path

        # bob is unreachable → queued, but the outbox copy must be signed.
        secured_client.post("/send", params={"to": "bob@10.0.0.2:8080/bob", "message": "hi"})
        base = Path(secured_client.app.extra.get("base_dir"))
        kr = KeyRing(base_dir=base)
        qpath = list((base / "queue").glob("*.pending"))
        assert qpath, "expected a queued send"
        pending_mail = json.loads(qpath[0].read_text())["mail"]
        m = Mail.from_dict(pending_mail)
        assert m.signature, "sent mail should be signed"
        assert m.verify_signature(kr) is True

    def test_receive_requires_signature_when_policy_on(self, secured_client):
        from agentmail.mail import Mail

        # Unsigned mail must be rejected (403)
        m = Mail(from_addr="peer@h/peer", to_addr="testagent@localhost", message="unsigned")
        resp = secured_client.post("/receive", json=m.to_dict())
        assert resp.status_code == 403

    def test_receive_accepts_signed_mail(self, secured_client):
        from agentmail.crypto import KeyRing
        from agentmail.mail import Mail

        base = Path(secured_client.app.extra.get("base_dir"))
        peer = secured_client.app.extra["peer_keyring"]
        m = Mail(from_addr="peer@h/peer", to_addr="testagent@localhost", message="signed hello")
        m.sign(peer)  # signed by peer, whom testagent trusts
        resp = secured_client.post("/receive", json=m.to_dict())
        assert resp.status_code == 200
        assert resp.json()["status"] == "received"

    def test_receive_decrypts_e2e_body(self, secured_client):
        from agentmail.crypto import KeyRing
        from agentmail.mail import Mail

        base = Path(secured_client.app.extra.get("base_dir"))
        peer = secured_client.app.extra["peer_keyring"]
        kr = KeyRing(base_dir=base)
        # sender 'peer' encrypts for US (testagent) using testagent's enc pubkey
        m = Mail(from_addr="peer@h/peer", to_addr="testagent@localhost", message="top secret")
        m.encrypt_for(kr.self_enc_public_pem())
        m.sign(peer)
        assert m.encrypted
        resp = secured_client.post("/receive", json=m.to_dict())
        assert resp.status_code == 200
        # After receive, the inbox copy should be decrypted cleartext
        inbox = secured_client.get("/inbox").json()
        assert inbox["count"] == 1
        read = secured_client.get("/read", params={"hash": inbox["messages"][0]["short_hash"]}).json()
        assert read["message"] == "top secret"
        assert read["encrypted"] in (False, "False")