"""AgentMail gateway adapter (task-mail bridge) — generic, any node.

A single program that turns AgentMail into work. It is transport- and
agent-agnostic: it speaks only AgentMail and hands task payloads to a
pluggable dispatch function supplied by the deploying node. The only thing
that differs between nodes is the dispatch logic + supervisor (systemd / s6 /
tmux) — that is a runtime detail, not a different spec.

Wire contract (protocol-level, see agentmail.mail.Mail):
    x-task-request  body {task_id, reply_to, payload:{type,args}, ttl?}
    x-task-result   body {task_id, status:ok|error|pending, result?, error?, agent?}

Run:
    python -m agentmail.bridge \
        --config ~/.agentmail/config.yaml \
        --poll 120 --max-iters 0 --once
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from .config import DEFAULT_CONFIG_PATH, load_config
from .mail import ContentType, Mail

# A dispatch function takes the parsed task dict and returns (result, error).
# Node operators supply this. The default echoes back the payload (smoke test).
DispatchFn = Callable[[dict], tuple[Any, Optional[str]]]

DEFAULT_POLL = 120
STOP_MATCHERS = (
    lambda low: low == "stop",
    lambda low: low.startswith("stop."),
    lambda low: "stop the loop" in low,
    lambda low: "stop the ping-pong" in low,
    lambda low: "end the loop" in low,
)


def is_stop(body: str) -> bool:
    """Hardened standalone-stop matcher (never trips on 'until STOP' prose)."""
    low = (body or "").strip().lower()
    return any(m(low) for m in STOP_MATCHERS)


def default_dispatch(task: dict) -> tuple[Any, Optional[str]]:
    """Smoke-test handler: acknowledge and return the received payload."""
    payload = task.get("payload", {})
    return {"echo": payload, "handled_by": "agentmail-bridge-default"}, None


def _my_address(config) -> str:
    http = config.transports.get("http")
    host = (http.host if http and http.host not in ("0.0.0.0",) else "localhost")
    port = http.port if http else 12345
    return f"{config.identity.name}@{host}:{port}/{config.identity.name}"


def run_once(
    base_url: str,
    config_path: Path,
    dispatch: DispatchFn,
    client: Optional[httpx.Client] = None,
) -> tuple[int, bool]:
    """Process one inbox sweep. Returns (tasks_handled, stop_requested).

    If ``client`` is provided it is used for HTTP calls (enables testing via an
    in-process ASGI transport); otherwise a real httpx.Client is created.
    """
    config = load_config(config_path)
    me = _my_address(config)
    handled = 0
    stop = False
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=30.0)
    try:
        inbox = client.get(f"{base_url}/inbox").json()
        for entry in inbox.get("messages", []):
            short = entry["short_hash"]
            mail_d = client.get(f"{base_url}/read", params={"hash": short}).json()
            body = mail_d.get("message", "")
            sender = mail_d.get("from", "")
            ct = mail_d.get("content-type", "text/plain")

            # Hardened STOP handling — only standalone stop ends the loop.
            if is_stop(body):
                stop = True
                # Acknowledge to the peer before exiting.
                client.post(
                    f"{base_url}/send",
                    params={"to": sender, "message": "STOP acknowledged — ending bridge."},
                )
                continue

            if ct != ContentType.APPLICATION_X_TASK_REQUEST.value:
                # Unknown content-type: log + leave in inbox would re-loop, so we
                # archive explicitly (server auto-archives on read already).
                continue

            try:
                task = json.loads(body)
            except json.JSONDecodeError as e:
                # Reply with an error result so the requester isn't left hanging.
                _reply_error(client, base_url, me, sender, task_id="?", error=f"bad json: {e}")
                continue

            task_id = task.get("task_id", str(uuid.uuid4()))
            reply_to = task.get("reply_to", sender)
            try:
                result, err = dispatch(task)
            except Exception as e:  # never crash the loop on a bad task
                result, err = None, f"dispatch exception: {e}"
                status = "error"
            else:
                status = "error" if err else "ok"

            reply = Mail.make_task_result(
                from_addr=me,
                to_addr=reply_to,
                task_id=task_id,
                status=status,
                result=result,
                error=err,
                agent=config.identity.name,
            )
            client.post(
                f"{base_url}/send",
                params={
                    "to": reply_to,
                    "message": reply.message,
                    "content_type": reply.content_type.value,
                },
            )
            handled += 1
    finally:
        if own_client:
            client.close()
    return handled, stop


def _reply_error(client, base_url, me, sender, task_id, error):
    reply = Mail.make_task_result(
        from_addr=me, to_addr=sender, task_id=task_id, status="error", error=error,
        agent=me.split("@")[0],
    )
    client.post(
        f"{base_url}/send",
        params={"to": sender, "message": reply.message, "content_type": reply.content_type.value},
    )


def main_loop(
    config_path: Path,
    base_url: str = "http://localhost:12345",
    poll: int = DEFAULT_POLL,
    max_iters: int = 0,
    dispatch: DispatchFn = default_dispatch,
) -> None:
    """Run the adapter loop until STOP or max_iters (0 = forever)."""
    print(f"agentmail-bridge started: poll={poll}s, max_iters={max_iters or 'inf'}, url={base_url}")
    it = 0
    while True:
        if max_iters and it >= max_iters:
            print("max_iters reached; stopping.")
            break
        try:
            handled, stop = run_once(base_url, config_path, dispatch)
            if handled:
                print(f"[iter {it}] handled {handled} task(s)")
        except Exception as e:
            print(f"[warn] sweep failed: {e}")
        if stop:
            print("STOP received. Ending bridge.")
            break
        it += 1
        time.sleep(poll)


def _cli() -> None:
    # When invoked via `agentmail bridge ...`, sys.argv[1] is "bridge".
    # Strip it so the bridge's own argparse only sees its real flags.
    import sys
    argv = sys.argv[1:]
    if argv and argv[0] == "bridge":
        argv = argv[1:]
    p = argparse.ArgumentParser(prog="agentmail-bridge", description="AgentMail task-mail gateway adapter")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="AgentMail config path")
    p.add_argument("--url", default="http://localhost:12345", help="Local AgentMail server URL")
    p.add_argument("--poll", type=int, default=DEFAULT_POLL, help="Poll interval seconds")
    p.add_argument("--max-iters", type=int, default=0, help="0 = run forever")
    p.add_argument("--once", action="store_true", help="Run a single sweep and exit (for testing)")
    args = p.parse_args(argv)

    cfg_path = Path(args.config)
    if args.once:
        handled, stop = run_once(args.url, cfg_path, default_dispatch)
        print(f"one-shot: handled {handled}, stop={stop}")
        sys.exit(0)
    main_loop(cfg_path, base_url=args.url, poll=args.poll, max_iters=args.max_iters)


if __name__ == "__main__":
    _cli()
