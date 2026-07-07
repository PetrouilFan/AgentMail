"""Tests for the finalize operational flow: ping metadata, config reload, TLS.

These exercise the server/CLI glue that makes AgentMail usable without
manual key copying. (The trust CLI + broadcast end-to-end path is covered by
the live ad-hoc smoke test.)
"""
import pytest
from fastapi.testclient import TestClient

from agentmail.crypto import KeyRing
from agentmail.server import create_app
from agentmail.config import (
    AgentMailConfig,
    IdentityConfig,
    TransportConfig,
    AgentEntry,
    save_config,
)
from agentmail.transport import HTTPTransport


@pytest.fixture
def pair_dirs(tmp_path):
    a = tmp_path / "A"
    b = tmp_path / "B"
    for d in (a, b):
        (d / "keys" / "known_agents").mkdir(parents=True)
    KeyRing(a).generate()
    KeyRing(b).generate()
    (a / "config.yaml").write_text(
        "identity:\n  name: A\n  public_key: ''\n"
        "transports:\n  http:\n    host: 127.0.0.1\n    port: 8731\n"
        "agents: {}\ndefaults:\n  content_type: text/plain\n  require_signature: false\n")
    (b / "config.yaml").write_text(
        "identity:\n  name: B\n  public_key: ''\n"
        "transports:\n  http:\n    host: 127.0.0.1\n    port: 8732\n"
        "agents: {}\ndefaults:\n  content_type: text/plain\n  require_signature: false\n")
    return a, b


class TestPingMetadata:
    def test_ping_returns_real_keys_and_address(self, pair_dirs):
        a, _ = pair_dirs
        app = create_app(config_path=a / "config.yaml", base_dir=a)
        c = TestClient(app)
        data = c.get("/ping").json()
        assert data["name"] == "A"
        assert data["address"] == "A@127.0.0.1:8731/A"
        assert data["public_key"] and "BEGIN PUBLIC KEY" in data["public_key"]
        assert data["encryption_key"] and "BEGIN PUBLIC KEY" in data["encryption_key"]
        assert data["tls"] is False


class TestConfigReload:
    def test_send_sees_trusted_agent_without_restart(self, pair_dirs):
        """A peer added to the config after app startup must be reachable."""
        a, b = pair_dirs
        app = create_app(config_path=a / "config.yaml", base_dir=a)
        c = TestClient(app)
        # Simulate `trust`: add B to A's translation table on disk.
        cfg = AgentMailConfig(
            identity=IdentityConfig(name="A"),
            transports={"http": TransportConfig(host="127.0.0.1", port=8731)},
            agents={"B": AgentEntry(address="B@127.0.0.1:8732/B", transport="http")},
        )
        save_config(cfg, a / "config.yaml")
        r = c.post("/send", params={"to": "B", "message": "hi", "from_addr": "A@127.0.0.1:8731/A"})
        assert r.status_code == 200
        # B's server is down → queued, but routing succeeded (no 404).
        assert r.json()["to"] == "B@127.0.0.1:8732/B"


class TestTLSTransport:
    def test_https_url_when_tls_enabled(self, pair_dirs):
        cfg = AgentMailConfig(
            identity=IdentityConfig(name="x"),
            transports={"http": TransportConfig(host="h", port=9, tls=True)},
        )
        t = HTTPTransport(cfg, tls=True)
        url = t._address_to_url("agent@host:99/agent", tls=True)
        assert url == "https://host:99"
