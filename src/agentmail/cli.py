"""AgentMail CLI — manual interaction with the AgentMail server.

Usage:
    agentmail send <to> <message> [--content-type TYPE] [--from ADDR] [--all]
    agentmail inbox
    agentmail outbox
    agentmail read <short_hash>
    agentmail archive <full_hash>
    agentmail ping
    agentmail trust <url>                — exchange keys with a peer + add to table
    agentmail init                         — initialize config directory
    agentmail keygen [--overwrite]         — generate local signing + encryption keys
    agentmail serve [--host HOST] [--port PORT] [--config PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx
import yaml

from .config import DEFAULT_CONFIG_PATH, init_config_dir, load_config
from .bridge import _cli as _bridge_cli


DEFAULT_URL = "http://localhost:12345"


def _base_url(args: argparse.Namespace) -> str:
    return getattr(args, "url", None) or DEFAULT_URL


def cmd_send(args: argparse.Namespace) -> None:
    """Send a message to an agent (or broadcast with --all)."""
    url = _base_url(args)
    if getattr(args, "all", False):
        # Broadcast to every agent in the translation table.
        from .config import load_config, DEFAULT_CONFIG_PATH
        cfg_path = Path(args.config) if getattr(args, "config", None) else DEFAULT_CONFIG_PATH
        config = load_config(cfg_path)
        if not config.agents:
            print("No agents in translation table. Add one with `agentmail trust <url>`.")
            sys.exit(1)
        ok = 0
        for name in config.agents:
            r = _do_send(url, name, args.message, args.content_type, args.from_addr)
            if r[0]:
                ok += 1
                print(f"✓ Sent to {name}")
            else:
                print(f"⏳ Queued (will retry): {name} — {r[1]}")
        print(f"\nBroadcast complete: {ok}/{len(config.agents)} delivered immediately.")
        return
    if not args.to or not args.message:
        print("Error: 'to' and 'message' are required unless using --all")
        sys.exit(1)
    status, err = _do_send(url, args.to, args.message, args.content_type, args.from_addr)
    if err and not status:
        print(f"⏳ Queued (delivery failed, will retry): {args.to}")
        print(f"  ⚠ Transport error: {err}")
    else:
        print(f"✓ Sent to {args.to}")
    print(f"  (see /outbox for mail_hash)")


def _do_send(url: str, to: str, message: str, content_type: str, from_addr: str | None):
    """Send one message; return (success, error)."""
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            f"{url}/send",
            params={
                "to": to,
                "message": message,
                "content_type": content_type,
                "from_addr": from_addr,
            },
        )
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text}"
        data = resp.json()
        return (not data.get("error")), data.get("error", "")


def cmd_inbox(args: argparse.Namespace) -> None:
    """List messages in inbox."""
    url = _base_url(args)
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{url}/inbox")
        if resp.status_code != 200:
            print(f"Error: {resp.status_code} — {resp.text}")
            sys.exit(1)
        data = resp.json()
        if data["count"] == 0:
            print("Inbox is empty.")
            return
        print(f"Inbox ({data['count']} messages):")
        print(f"{'#':<4} {'From':<40} {'Hash':<20}")
        print("-" * 64)
        for i, msg in enumerate(data["messages"]):
            print(f"{i:<4} {msg['from']:<40} {msg['short_hash']:<20}")


def cmd_outbox(args: argparse.Namespace) -> None:
    """List sent messages and pending (retrying) sends."""
    url = _base_url(args)
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{url}/outbox")
        if resp.status_code != 200:
            print(f"Error: {resp.status_code} — {resp.text}")
            sys.exit(1)
        data = resp.json()
        if data["sent_count"] == 0 and data["pending_count"] == 0:
            print("Outbox is empty.")
            return
        if data["sent_count"]:
            print(f"Sent ({data['sent_count']}):")
            print(f"{'#':<4} {'To':<40} {'Hash':<20}")
            print("-" * 64)
            for i, msg in enumerate(data["sent"]):
                print(f"{i:<4} {msg['to']:<40} {msg['short_hash']:<20}")
        if data["pending_count"]:
            print()
            print(f"Pending retry ({data['pending_count']}):")
            print(f"{'#':<4} {'To':<36} {'Hash':<18} {'Attempts':<9} Next")
            print("-" * 78)
            for i, p in enumerate(data["pending"]):
                print(
                    f"{i:<4} {p['to']:<36} {p['short_hash']:<18} "
                    f"{p['attempts']:<9} {p['next_attempt']}"
                )


def cmd_read(args: argparse.Namespace) -> None:
    """Read a full message by short hash."""
    url = _base_url(args)
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{url}/read", params={"hash": args.hash})
        if resp.status_code != 200:
            print(f"Error: {resp.status_code} — {resp.text}")
            sys.exit(1)
        data = resp.json()
        print(f"From:           {data['from']}")
        print(f"To:             {data['to']}")
        print(f"At:             {data['at']}")
        print(f"ID:             {data['id']}")
        print(f"Content-Type:   {data.get('content_type', 'text/plain')}")
        print(f"Hash:           {data['full_hash'][:16]}... ({len(data['full_hash'])} chars)")
        print()
        print(data["message"])


def cmd_archive(args: argparse.Namespace) -> None:
    """Archive a message by full hash."""
    url = _base_url(args)
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(f"{url}/archive", params={"hash": args.hash})
        if resp.status_code != 200:
            print(f"Error: {resp.status_code} — {resp.text}")
            sys.exit(1)
        data = resp.json()
        print(f"✓ Archived message {data['hash']}")


def cmd_ping(args: argparse.Namespace) -> None:
    """Ping a remote agent for metadata."""
    url = _base_url(args)
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(f"{url}/ping")
        if resp.status_code != 200:
            print(f"Error: {resp.status_code} — {resp.text}")
            sys.exit(1)
        data = resp.json()
        print(f"Name:          {data.get('name', 'unknown')}")
        print(f"Version:       {data.get('version', '?')}")
        print(f"Transports:    {', '.join(data.get('transports', []))}")
        print(f"TLS:           {data.get('tls', False)}")
        print(f"Address:       {data.get('address', 'unknown')}")
        print(f"Public Key:    {data.get('public_key', 'none')[:40]}…" if data.get("public_key") else "Public Key:    none")
        print(f"Capabilities:  {', '.join(data.get('capabilities', [])) or 'none'}")


def cmd_trust(args: argparse.Namespace) -> None:
    """Establish trust with a remote agent: import its keys + add to table.

    Pings the peer, writes its signing/encryption public keys into our
    known_agents/ keyring, and records the peer in our translation table so we
    can address it by name. This replaces manual .pub/.xpub file exchange.
    """
    from .crypto import KeyRing
    from .config import load_config, save_config, AgentEntry, DEFAULT_CONFIG_PATH

    url = _base_url(args)
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(f"{url}/ping")
        if resp.status_code != 200:
            print(f"Error: {resp.status_code} — {resp.text}")
            sys.exit(1)
        meta = resp.json()

    name = meta.get("name")
    if not name:
        print("Error: peer returned no name")
        sys.exit(1)
    signing_pub = meta.get("public_key")
    enc_pub = meta.get("encryption_key")
    if not signing_pub or not enc_pub:
        print("Error: peer has no identity keys (ask them to run `agentmail keygen`)")
        sys.exit(1)

    cfg_path = Path(args.config) if getattr(args, "config", None) else DEFAULT_CONFIG_PATH
    # Keyring root follows the config dir's parent — same as the server's base_dir.
    kr = KeyRing(base_dir=cfg_path.parent)
    kr.add_known_agent(name, signing_pub.encode("ascii"), enc_pub.encode("ascii"))
    print(f"✓ Trusted {name}: imported signing + encryption keys")

    # Record the peer in our translation table.
    cfg_path = Path(args.config) if getattr(args, "config", None) else DEFAULT_CONFIG_PATH
    config = load_config(cfg_path)
    address = meta.get("address") or f"{name}@{url.split('://')[-1].split('/')[0]}/{name}"
    transport = "https" if meta.get("tls") else "http"
    config.agents[name] = AgentEntry(address=address, transport=transport, public_key=signing_pub)
    save_config(config, cfg_path)
    print(f"✓ Added '{name}' to translation table → {address} ({transport})")


def cmd_mesh(args: argparse.Namespace) -> None:
    """Manage the mesh roster: import / export peers in one shot.

    Protocol-general: any node can generate or consume a roster of PUBLIC
    keys. Importing merges every entry into the translation table + keyring
    (idempotent). Never touches private keys.
    """
    from .config import (
        load_config,
        save_config,
        AgentEntry,
        DEFAULT_CONFIG_PATH,
    )
    from .crypto import KeyRing

    cfg_path = Path(args.config) if getattr(args, "config", None) else DEFAULT_CONFIG_PATH

    if args.mesh_action == "export":
        out = Path(args.out) if args.out else Path("mesh.yaml")
        from .mesh_cli import mesh_export
        mesh_export(cfg_path, out)
        return

    # import
    roster_path = Path(args.file)
    if not roster_path.exists():
        print(f"Error: roster file not found: {roster_path}")
        sys.exit(1)
    with open(roster_path) as f:
        roster = yaml.safe_load(f) or {}
    agents = roster.get("agents", {})
    if not agents:
        print("Error: roster contains no 'agents'")
        sys.exit(1)

    kr = KeyRing(base_dir=cfg_path.parent)
    kr.ensure()
    config = load_config(cfg_path)
    added = 0
    for name, a in agents.items():
        signing = (a.get("signing_key") or "").strip()
        enc = (a.get("encryption_key") or "").strip()
        if not signing:
            continue
        if not args.no_keys and enc:
            kr.add_known_agent(name, signing.encode("ascii"), enc.encode("ascii"))
        address = a.get("address") or f"{name}@localhost:12345/{name}"
        transport = a.get("transport") or ("https" if "https" in address else "http")
        config.agents[name] = AgentEntry(
            address=address, transport=transport, public_key=signing
        )
        added += 1
        print(f"✓ Imported '{name}' → {address} ({transport})"
              + ("  [keys]" if (not args.no_keys and enc) else "  [table only]"))

    save_config(config, cfg_path)
    scheme = "merge" if args.no_keys else "merge (keys + table)"
    print(f"\nImported {added} agent(s) into {cfg_path} ({scheme}).")


def cmd_init(args: argparse.Namespace) -> None:
    """Initialize the AgentMail config directory."""
    config_dir = init_config_dir()
    print(f"✓ Initialized AgentMail config at {config_dir}")
    print(f"  Edit {config_dir / 'config.yaml'} to configure agents and transports.")


def cmd_keygen(args: argparse.Namespace) -> None:
    """Generate or regenerate the local cryptographic identity."""
    from .crypto import KeyRing

    # Keyring root follows --config's parent (or default ~/.agentmail), so keys
    # land where the server (which uses base_dir = config.parent) will read them.
    cfg_path = Path(args.config) if getattr(args, "config", None) else DEFAULT_CONFIG_PATH
    kr = KeyRing(base_dir=cfg_path.parent)
    existed = kr.has_identity()
    kr.generate(overwrite=args.overwrite)
    print(f"✓ {'Regenerated' if existed and args.overwrite else 'Generated'} identity at {kr.keys_dir}")
    print(f"  Signing pub:  {kr.keys_dir / 'self.pub'}")
    print(f"  Enc pub:      {kr.keys_dir / 'self.xpub'}")
    print("  Share the .pub / .xpub files with agents you trust (add to their known_agents/).")


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the AgentMail server."""
    import uvicorn
    from pathlib import Path as _Path

    config_path = _Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    config = load_config(config_path)
    # Mailbox + keyring root follows the config file's directory, so a custom
    # --config X/config.yaml keeps its keys/mail in X/ (not ~/.agentmail).
    base_dir = config_path.parent
    bind_host = args.host or "0.0.0.0"
    port = args.port or 12345
    http_conf = config.transports.get("http")
    if http_conf and not args.port:
        port = http_conf.port

    # TLS termination: if the transport declares tls:true (and certs are
    # present), serve over HTTPS. Cert paths come from the config or flags.
    # uvicorn 0.46 takes ssl_certfile/ssl_keyfile (not ssl_context).
    ssl_kwargs: dict = {}
    if (http_conf and http_conf.tls) or args.tls:
        certfile = args.certfile or (http_conf and getattr(http_conf, "certfile", None))
        keyfile = args.keyfile or (http_conf and getattr(http_conf, "keyfile", None))
        if not certfile or not keyfile:
            print("Error: TLS enabled but certfile/keyfile not provided. "
                  "Pass --certfile/--keyfile or set tls.certfile in config.")
            sys.exit(1)
        ssl_kwargs = {"ssl_certfile": certfile, "ssl_keyfile": keyfile}
        print(f"  TLS: enabled (cert={certfile})")

    print(f"Starting AgentMail server on {bind_host}:{port}")
    print(f"  Identity: {config.identity.name}")
    print(f"  Config: {config_path}")

    # Import the app factory — use the config_path if specified
    from .server import create_app
    app = create_app(config_path=config_path, base_dir=base_dir)
    uvicorn.run(app, host=bind_host, port=port, **ssl_kwargs)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="agentmail",
        description="AgentMail — The messaging protocol for AI agents",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Server URL (default: http://localhost:12345)")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # send
    p_send = sub.add_parser("send", help="Send a message to an agent")
    p_send.add_argument("to", nargs="?", help="Recipient agent ID or full address (omit with --all)")
    p_send.add_argument("message", help="Message body")
    p_send.add_argument("--content-type", default="text/plain", help="Content type")
    p_send.add_argument("--from-addr", default=None, dest="from_addr", help="Sender address")
    p_send.add_argument("--all", action="store_true", help="Broadcast to all agents in translation table")
    p_send.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Config file with translation table")

    # inbox
    sub.add_parser("inbox", help="List messages in inbox")

    # outbox
    sub.add_parser("outbox", help="List sent messages and pending retries")

    # read
    p_read = sub.add_parser("read", help="Read a message by short hash")
    p_read.add_argument("hash", help="Short hash (16 chars)")

    # archive
    p_archive = sub.add_parser("archive", help="Archive a message by full hash")
    p_archive.add_argument("hash", help="Full hash (64 chars)")

    # ping
    sub.add_parser("ping", help="Ping a remote agent for metadata")

    # trust
    p_trust = sub.add_parser("trust", help="Establish trust with a remote agent (exchange keys + add to table)")
    p_trust.add_argument("url", help="Base URL of the agent to trust (e.g. http://host:12345)")
    p_trust.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Config file to update")

    # mesh
    p_mesh = sub.add_parser("mesh", help="Manage the mesh roster (import/export peers)")
    p_mesh.add_argument("mesh_action", choices=["import", "export"], help="import a roster or export one")
    p_mesh.add_argument("file", nargs="?", default=None, help="Roster YAML file (for import; for export: --out)")
    p_mesh.add_argument("--out", default=None, help="Output path for export (default: mesh.yaml)")
    p_mesh.add_argument("--no-keys", action="store_true", help="Import translation table only (skip keyring keys)")
    p_mesh.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Config file to update")

    # init
    sub.add_parser("init", help="Initialize config directory")

    # keygen
    p_keygen = sub.add_parser("keygen", help="Generate local signing + encryption keys")
    p_keygen.add_argument("--overwrite", action="store_true", help="Overwrite existing keys")
    p_keygen.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Config dir parent for keyring")

    # serve
    p_serve = sub.add_parser("serve", help="Start the AgentMail server")
    p_serve.add_argument("--host", default="0.0.0.0", help="Bind host")
    p_serve.add_argument("--port", type=int, default=None, help="Bind port (default: from config transports.http.port or 12345)")
    p_serve.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Config file path")
    p_serve.add_argument("--tls", action="store_true", help="Enable TLS (also enabled if config transports.http.tls:true)")
    p_serve.add_argument("--certfile", default=None, help="TLS certificate file (PEM)")
    p_serve.add_argument("--keyfile", default=None, help="TLS private key file (PEM)")

    # bridge (gateway adapter — generic per-node task-mail handler)
    p_bridge = sub.add_parser("bridge", help="Run the gateway adapter (task-mail bridge)")
    p_bridge.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="AgentMail config path")
    p_bridge.add_argument("--url", default="http://localhost:12345", help="Local AgentMail server URL")
    p_bridge.add_argument("--poll", type=int, default=120, help="Poll interval seconds")
    p_bridge.add_argument("--max-iters", type=int, default=0, help="0 = run forever")
    p_bridge.add_argument("--once", action="store_true", help="Run a single sweep and exit (for testing)")

    args = parser.parse_args()

    commands = {
        "send": cmd_send,
        "inbox": cmd_inbox,
        "outbox": cmd_outbox,
        "read": cmd_read,
        "archive": cmd_archive,
        "ping": cmd_ping,
        "trust": cmd_trust,
        "mesh": cmd_mesh,
        "init": cmd_init,
        "keygen": cmd_keygen,
        "serve": cmd_serve,
        "bridge": lambda args: _bridge_cli(),
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()