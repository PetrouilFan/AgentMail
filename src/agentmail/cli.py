"""AgentMail CLI — manual interaction with the AgentMail server.

Usage:
    agentmail send <to> <message> [--content-type TYPE] [--from ADDR]
    agentmail inbox
    agentmail outbox
    agentmail read <short_hash>
    agentmail archive <full_hash>
    agentmail ping
    agentmail init                         — initialize config directory
    agentmail keygen [--overwrite]         — generate local signing + encryption keys
    agentmail serve [--host HOST] [--port PORT]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from .config import DEFAULT_CONFIG_PATH, init_config_dir, load_config


DEFAULT_URL = "http://localhost:8080"


def _base_url(args: argparse.Namespace) -> str:
    return getattr(args, "url", None) or DEFAULT_URL


def cmd_send(args: argparse.Namespace) -> None:
    """Send a message to an agent."""
    url = _base_url(args)
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            f"{url}/send",
            params={
                "to": args.to,
                "message": args.message,
                "content_type": args.content_type,
                "from_addr": args.from_addr,
            },
        )
        if resp.status_code != 200:
            print(f"Error: {resp.status_code} — {resp.text}")
            sys.exit(1)
        data = resp.json()
        if data.get("error"):
            print(f"⏳ Queued (delivery failed, will retry): {data['to']}")
        else:
            print(f"✓ Sent to {data['to']}")
        print(f"  mail_hash: {data['mail_hash']}")
        print(f"  full_hash: {data['full_hash']}")
        if data.get("error"):
            print(f"  ⚠ Transport error: {data['error']}")


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
        print(f"Content-Type:   {data.get('content-type', 'text/plain')}")
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
        print(f"Public Key:    {data.get('public_key', 'none')}")
        print(f"Capabilities:  {', '.join(data.get('capabilities', [])) or 'none'}")


def cmd_init(args: argparse.Namespace) -> None:
    """Initialize the AgentMail config directory."""
    config_dir = init_config_dir()
    print(f"✓ Initialized AgentMail config at {config_dir}")
    print(f"  Edit {config_dir / 'config.yaml'} to configure agents and transports.")


def cmd_keygen(args: argparse.Namespace) -> None:
    """Generate or regenerate the local cryptographic identity."""
    from .crypto import KeyRing

    kr = KeyRing()
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
    port = args.port or 8080
    http_conf = config.transports.get("http")
    if http_conf and not args.port:
        port = http_conf.port

    print(f"Starting AgentMail server on {bind_host}:{port}")
    print(f"  Identity: {config.identity.name}")
    print(f"  Config: {config_path}")

    # Import the app factory — use the config_path if specified
    from .server import create_app
    app = create_app(config_path=config_path, base_dir=base_dir)
    uvicorn.run(app, host=bind_host, port=port)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="agentmail",
        description="AgentMail — The messaging protocol for AI agents",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Server URL (default: http://localhost:8080)")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # send
    p_send = sub.add_parser("send", help="Send a message to an agent")
    p_send.add_argument("to", help="Recipient agent ID or full address")
    p_send.add_argument("message", help="Message body")
    p_send.add_argument("--content-type", default="text/plain", help="Content type")
    p_send.add_argument("--from-addr", default=None, dest="from_addr", help="Sender address")

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

    # init
    sub.add_parser("init", help="Initialize config directory")

    # keygen
    p_keygen = sub.add_parser("keygen", help="Generate local signing + encryption keys")
    p_keygen.add_argument("--overwrite", action="store_true", help="Overwrite existing keys")

    # serve
    p_serve = sub.add_parser("serve", help="Start the AgentMail server")
    p_serve.add_argument("--host", default="0.0.0.0", help="Bind host")
    p_serve.add_argument("--port", type=int, default=8080, help="Bind port")
    p_serve.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Config file path")

    args = parser.parse_args()

    commands = {
        "send": cmd_send,
        "inbox": cmd_inbox,
        "outbox": cmd_outbox,
        "read": cmd_read,
        "archive": cmd_archive,
        "ping": cmd_ping,
        "init": cmd_init,
        "keygen": cmd_keygen,
        "serve": cmd_serve,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()