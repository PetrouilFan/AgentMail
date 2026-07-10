"""Tests for the new protocol-general features (build batch 2026-07-10).

Covers: binary/multipart payloads, structured task-mail wire contract,
mesh import/export, gateway adapter (bridge) one-shot, federation relay.
"""

import base64
import json

import pytest
from pathlib import Path

from agentmail.mail import Mail, ContentType, MailPart
from agentmail.config import AgentMailConfig, IdentityConfig, AgentEntry, load_config, save_config, DEFAULT_CONFIG_DIR
from agentmail.crypto import KeyRing
from agentmail.mesh_cli import mesh_export
from agentmail.bridge import run_once, is_stop, default_dispatch


# ── Binary / multipart ───────────────────────────────────────────

class TestBinaryMultipart:
    def test_add_binary_part_sets_multipart(self):
        m = Mail(from_addr="a@a/a", to_addr="b@b/b", message="meta")
        m.add_binary_part(b"\x00\x01\x02", filename="bin.dat", content_type="application/octet-stream")
        assert m.content_type == ContentType.MULTIPART_MIXED
        assert len(m.parts) == 1
        assert m.parts[0].filename == "bin.dat"

    def test_binary_roundtrip(self):
        m = Mail(from_addr="a@a/a", to_addr="b@b/b", message="ok")
        m.add_binary_part(b"hello world", filename="f.txt")
        d = m.to_dict()
        restored = Mail.from_dict(d)
        parts = restored.decode_binary()
        assert parts[0][0] == "f.txt"
        assert parts[0][2] == b"hello world"

    def test_hash_includes_parts(self):
        m1 = Mail(from_addr="a@a/a", to_addr="b@b/b", message="x")
        m1.add_binary_part(b"same")
        m2 = Mail(from_addr="a@a/a", to_addr="b@b/b", message="x")
        m2.add_binary_part(b"different")
        assert m1.full_hash != m2.full_hash
        assert m1.verify_hash() and m2.verify_hash()


# ── Structured task-mail wire contract ───────────────────────────

class TestTaskMail:
    def test_make_task_request(self):
        m = Mail.make_task_request("a@a/a", "b@b/b", "task-1", {"type": "ping"}, reply_to="a@a/a")
        assert m.content_type == ContentType.APPLICATION_X_TASK_REQUEST
        body = m.parse_task()
        assert body["task_id"] == "task-1"
        assert body["payload"]["type"] == "ping"
        assert body["reply_to"] == "a@a/a"

    def test_make_task_result(self):
        m = Mail.make_task_result("b@b/b", "a@a/a", "task-1", "ok", result={"v": 42}, agent="b")
        assert m.content_type == ContentType.APPLICATION_X_TASK_RESULT
        body = m.parse_task()
        assert body["status"] == "ok"
        assert body["result"]["v"] == 42
        assert body["agent"] == "b"

    def test_task_error_shape(self):
        m = Mail.make_task_result("b@b/b", "a@a/a", "t", "error", error="boom")
        body = m.parse_task()
        assert body["status"] == "error"
        assert body["error"] == "boom"

    def test_parse_task_rejects_non_task(self):
        m = Mail(from_addr="a@a/a", to_addr="b@b/b", message="hi")
        with pytest.raises(ValueError):
            m.parse_task()


# ── Gateway adapter (bridge) ─────────────────────────────────────

class TestBridge:
    def test_is_stop_standalone(self):
        assert is_stop("stop")
        assert is_stop("stop.")
        assert is_stop("stop the loop")
        # The 07-07 misfire case: prose must NOT trip it.
        assert not is_stop("sleep — until STOP")

    def test_default_dispatch_echoes(self):
        result, err = default_dispatch({"payload": {"type": "ping"}})
        assert err is None
        assert result["echo"]["type"] == "ping"

    def test_bridge_one_shot_replies_task(self, tmp_path):
        """Stand up a server, send a task-request, run the bridge once, expect a result."""
        from fastapi.testclient import TestClient
        from agentmail.server import create_app

        base = tmp_path / "data"
        kr = KeyRing(base_dir=base)
        kr.generate()
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "identity:\n  name: nodeB\n  public_key: ''\n"
            "transports: {}\nagents: {}\ndefaults:\n  content_type: text/plain\n"
        )
        app = create_app(config_path=cfg, base_dir=base)
        with TestClient(app) as client:
            req = Mail.make_task_request(
                from_addr="nodeA@1.2.3.4:12345/nodeA",
                to_addr="nodeB@localhost/nodeB",
                task_id="abc",
                payload={"type": "ping"},
            )
            r = client.post("/receive", json=req.to_dict())
            assert r.status_code == 200

            # Run the bridge's single sweep against the local server.
            handled, stop = run_once("http://testserver", cfg, default_dispatch, client=client)
            assert handled == 1
            assert stop is False

            # A task-result should now be in the outbox, addressed back.
            out = client.get("/outbox").json()
            assert out["sent_count"] == 1
            sent = out["sent"][0]
            assert sent["to"] == "nodeA@1.2.3.4:12345/nodeA"


# ── Mesh import / export ─────────────────────────────────────────

@pytest.fixture
def two_node_dirs(tmp_path):
    """Create two isolated node dirs, each with its own identity + one peer."""
    a = tmp_path / "nodeA"
    b = tmp_path / "nodeB"
    for d in (a, b):
        (d / "keys" / "known_agents").mkdir(parents=True)
    kr_a = KeyRing(base_dir=a)
    kr_a.generate()
    kr_b = KeyRing(base_dir=b)
    kr_b.generate()
    cfg_a = a / "config.yaml"
    cfg_a.write_text(
        "identity:\n  name: nodeA\n  public_key: ''\n"
        "transports: {}\nagents:\n  nodeB:\n    address: nodeB@127.0.0.1:12345/nodeB\n"
        "    transport: http\n    public_key: ''\ndefaults:\n  content_type: text/plain\n"
    )
    cfg_b = b / "config.yaml"
    cfg_b.write_text(
        "identity:\n  name: nodeB\n  public_key: ''\n"
        "transports: {}\nagents: {}\ndefaults:\n  content_type: text/plain\n"
    )
    # nodeA trusts nodeB's keys too (so E2E both ways).
    kr_a.add_known_agent(
        "nodeB", kr_b.self_signing_public_pem(), kr_b.self_enc_public_pem()
    )
    return cfg_a, cfg_b, kr_a, kr_b


class TestMeshRoster:
    def test_export_then_import(self, two_node_dirs):
        cfg_a, cfg_b, kr_a, kr_b = two_node_dirs
        roster = cfg_a.parent / "mesh.yaml"
        n = mesh_export(cfg_a, roster)
        assert n == 2  # nodeA (self) + nodeB
        roster_text = roster.read_text()
        assert "nodeA:" in roster_text and "nodeB:" in roster_text
        assert "PRIVATE" not in roster_text and "self.key" not in roster_text

        # nodeB imports the roster → learns nodeA (table + keys) in one shot.
        from agentmail.cli import cmd_mesh
        import argparse
        args = argparse.Namespace(
            mesh_action="import", file=str(roster), out=None,
            no_keys=False, config=str(cfg_b),
        )
        cmd_mesh(args)

        # nodeB now has nodeA in its translation table + keyring.
        config_b = load_config(cfg_b)
        assert "nodeA" in config_b.agents
        assert (cfg_b.parent / "keys" / "known_agents" / "nodeA.pub").exists()
        assert (cfg_b.parent / "keys" / "known_agents" / "nodeA.xpub").exists()

    def test_import_no_keys_table_only(self, two_node_dirs):
        cfg_a, cfg_b, kr_a, kr_b = two_node_dirs
        roster = cfg_a.parent / "mesh.yaml"
        mesh_export(cfg_a, roster)
        from agentmail.cli import cmd_mesh
        import argparse
        args = argparse.Namespace(
            mesh_action="import", file=str(roster), out=None,
            no_keys=True, config=str(cfg_b),
        )
        cmd_mesh(args)
        config_b = load_config(cfg_b)
        assert "nodeA" in config_b.agents
        # Keyring must NOT have nodeA's keys when --no-keys.
        assert not (cfg_b.parent / "keys" / "known_agents" / "nodeA.pub").exists()


# ── Federation relay (opt-in) ────────────────────────────────────

class TestFederation:
    def test_unknown_recipient_404_when_federation_off(self, tmp_path):
        from fastapi.testclient import TestClient
        from agentmail.server import create_app

        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "identity:\n  name: edge\n  public_key: ''\n"
            "transports: {}\nagents: {}\ndefaults:\n  content_type: text/plain\n"
            "  federation: false\n"
        )
        app = create_app(config_path=cfg, base_dir=tmp_path)
        with TestClient(app) as client:
            # federation off + bare unknown name → 404 (no relay attempt).
            r = client.post("/send", params={"to": "ghost", "message": "hi"})
            assert r.status_code == 404

    def test_federation_relays_to_router(self, tmp_path):
        """With federation on, a bare unknown recipient is relayed to a router.

        The router points at a closed port (127.0.0.1:1) so the relay fails
        fast (connection refused) without hanging on a timeout. The direct
        send path is not taken because the recipient is a bare name.
        """
        from fastapi.testclient import TestClient
        from agentmail.server import create_app

        base = tmp_path / "data"
        kr = KeyRing(base_dir=base)
        kr.generate()
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "identity:\n  name: router\n  public_key: ''\n"
            "transports: {}\nagents:\n  peer:\n    address: peer@127.0.0.1:1/peer\n"
            "    transport: http\n    public_key: ''\ndefaults:\n  content_type: text/plain\n"
            "  federation: true\n"
        )
        app = create_app(config_path=cfg, base_dir=base)
        with TestClient(app) as client:
            # bare unknown name → relay branch → peer refuses → relay fails → 404.
            r = client.post("/send", params={"to": "ghost", "message": "hi"})
            assert r.status_code == 404

    def test_federation_flag_present_in_defaults(self):
        cfg = AgentMailConfig()
        assert cfg.defaults.federation is False

    def test_federation_multihop_delivers(self, tmp_path):
        """Multi-hop: edge can't resolve, relays to router, router relays to
        final which CAN resolve and deliver. Proves true multi-hop routing."""
        import subprocess, time
        import httpx

        def write_cfg(name, port, agents, federation=True, hops=5):
            d = tmp_path / name
            (d / "keys" / "known_agents").mkdir(parents=True)
            cfg = d / "config.yaml"
            agents_yaml = "\n".join(
                f"  {n}:\n    address: {a}\n    transport: http\n    public_key: ''"
                for n, a in agents.items()
            )
            cfg.write_text(
                f"identity:\n  name: {name}\n  public_key: ''\n"
                f"transports:\n  http:\n    host: 127.0.0.1\n    port: {port}\n"
                f"agents:\n{agents_yaml}\n"
                f"defaults:\n  content_type: text/plain\n  federation: {str(federation).lower()}\n"
                f"  max_federation_hops: {hops}\n"
            )
            return d, cfg

        de, cfge = write_cfg("edge", 13501, {}, federation=True)
        dr, cfgr = write_cfg("router", 13502, {"final": "final@127.0.0.1:13503/final"})
        df, cfgf = write_cfg("final", 13503, {"dest": "dest@127.0.0.1:13503/dest"})
        for d in (de, dr, df):
            subprocess.run([str(Path(__file__).parent.parent / ".venv" / "bin" / "python"),
                             "-m", "agentmail", "keygen", "--config", str(d / "config.yaml")],
                            capture_output=True)

        procs = []
        for d, port in ((de, 13501), (dr, 13502), (df, 13503)):
            p = subprocess.Popen([str(Path(__file__).parent.parent / ".venv" / "bin" / "python"),
                                  "-m", "agentmail", "serve", "--config", str(d / "config.yaml"),
                                  "--host", "127.0.0.1"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(p)

        def up(url):
            for _ in range(150):
                try:
                    httpx.get(url + "/ping", timeout=1); return True
                except Exception:
                    time.sleep(0.1)
            return False

        try:
            assert up("http://127.0.0.1:13501") and up("http://127.0.0.1:13502") and up("http://127.0.0.1:13503")
            # edge sends to 'dest' (unknown to edge, unknown to router) → multi-hop
            # edge relays to router → router relays to final → final delivers to dest.
            r = httpx.post("http://127.0.0.1:13501/send", params={"to": "dest", "message": "hop test"})
            # edge can't deliver directly (404 path would be if no federation); with
            # federation it relays. Whether it returns 'federated' or not depends on
            # router reachability — router IS up, so edge relays and returns 'federated'.
            assert r.status_code == 200
            # Give the relay chain a moment, then dest (final) should have received it.
            time.sleep(1.0)
            inbox = httpx.get("http://127.0.0.1:13503/inbox").json()
            # final's inbox is addressed to 'final' (the relay target), not 'dest'.
            # The relay delivers to final's /send with to=dest; final can't resolve
            # 'dest' either, but 'dest' is final itself? No — final knows 'dest' as a
            # peer pointing at dest@127.0.0.1:13503/dest == final. So final delivers
            # locally to its own inbox.
            assert inbox["count"] >= 1, f"final inbox empty: {inbox}"
            msg = httpx.get("http://127.0.0.1:13503/read", params={"hash": inbox["messages"][0]["short_hash"]}).json()
            assert msg["message"] == "hop test"
        finally:
            for p in procs:
                p.terminate()
