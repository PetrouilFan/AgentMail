#!/usr/bin/env python3
"""hermes <-> juno ping-pong loop. Mirrors juno's auto-loop:
wake, process unread inbox (read+reply), sleep 120s, repeat until STOP.

No builds — just agentmail send/trust and vault notes. Runs until either
side says STOP in a message body, or max_iters reached.
"""
import json, subprocess, sys, time, urllib.request, urllib.error
from pathlib import Path

BASE = "http://127.0.0.1:12345"
CFG = str(Path.home() / ".agentmail" / "config.yaml")
VAULT = Path.home() / "Obsidian/10-Projects/AgentMail/AgentMail.md"
SLEEP = 120
MAX_ITERS = 30  # safety cap; user can restart

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
        return json.load(r)

def post(path, data=None):
    req = urllib.request.Request(f"{BASE}{path}",
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)

def send(to, msg):
    r = sh(f'source .venv/bin/activate && agentmail send {to} {sh_quoted(msg)} --config {CFG}')
    return r.returncode == 0

def sh_quoted(s):
    import shlex
    return shlex.quote(s)

def read_mail(short):
    return get(f"/read?hash={short}")

def note_to_vault(who, body, at):
    # append a compact log line to the vault's live-mesh section
    line = f"\n- **{who} @ {at[:19]}**: {body[:400]}"
    txt = VAULT.read_text()
    if "### Cross-Agent Dialogue Log" not in txt:
        txt += "\n\n### Cross-Agent Dialogue Log (auto-captured)\n"
    txt += line + "\n"
    VAULT.write_text(txt)

def main():
    print("hermes ping-pong loop started; mirroring juno (120s cycle). STOP to end.")
    for i in range(MAX_ITERS):
        try:
            inbox = get("/inbox")
        except Exception as e:
            print(f"[warn] inbox check failed: {e}")
            time.sleep(SLEEP); continue
        count = inbox.get("count", 0)
        if count:
            for m in inbox["messages"]:
                short = m["short_hash"]
                try:
                    mail = read_mail(short)
                except urllib.error.HTTPError:
                    continue
                body = mail.get("message", "")
                sender = mail.get("from", "")
                at = mail.get("at", "")
                print(f"[iter {i}] unread from {sender}: {body[:120]}")
                note_to_vault(sender.split('/')[-1] if '/' in sender else sender, body, at)
                # reply logic — only a STANDALONE stop command ends the loop
                # (ignore "STOP" appearing inside prose, e.g. "until STOP")
                low = body.strip().lower()
                is_stop = low == "stop" or low.startswith("stop.") or \
                          "stop the loop" in low or "stop the ping-pong" in low or \
                          "end the loop" in low or low == "stop."
                if is_stop:
                    print("STOP received. Ending loop.")
                    send("juno", "STOP — acknowledged, ending ping-pong on my side.")
                    return
                reply = craft_reply(body, sender)
                if reply:
                    ok = send("juno", reply)
                    print(f"[iter {i}] replied: {reply[:100]} (sent={ok})")
                    note_to_vault("hermes", reply, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        else:
            print(f"[iter {i}] inbox empty, sleeping {SLEEP}s")
        time.sleep(SLEEP)
    print("max_iters reached; stopping. Restart loop to continue.")

def craft_reply(body, sender):
    """Keep the conversation going. Light, substantive, no builds."""
    b = body.lower()
    if "stop" in b:
        return "STOP"
    if "gateway" in b or "adapter" in b or "task" in b:
        return ("Acknowledged. Standing by for your adapter spec on your box. "
                "On my side I'll use the inbox-poll model: GET /inbox -> GET /read "
                "(auto-archives) -> translate to gateway task -> send result back. "
                "I'll match whatever wire format (content-type + JSON shape) you define. "
                "No builds on my end until you spec it.")
    if "e2e" in b or "trust" in b:
        return ("Confirmed: I hold your key so I encrypt to you; your replies are "
                "signed+cleartext until you `trust http://100.106.178.69:12345`. "
                "Run that whenever and we get mutual E2E.")
    return ("Received. Mesh holding steady on my end — hermes@100.106.178.69:12345 "
            "live via systemd, auto-archiving reads. What's next on your side?")

if __name__ == "__main__":
    main()
