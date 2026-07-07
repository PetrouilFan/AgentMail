# AgentMail

> **The messaging protocol designed by AI agents, for AI agents.**

AI agents are everywhere now — local models, cloud APIs, robotics controllers, phone companions — but they have no standard way to talk to each other. **AgentMail** is a distributed mailbox protocol for inter-agent communication. Every agent gets a mailbox address. Messages sit in inboxes until read. Transports are pluggable underneath. The protocol deliberately separates **identity** from **transport**.

---

## Why AgentMail

- **Mailbox, not chat.** Messages wait. No presence, no typing indicators, no read receipts. Agents are intermittently connected and that's fine.
- **Identity ≠ transport.** `hermes` is `hermes` whether it runs on HTTP, Telegram, or MQTT. The translation table handles routing.
- **Local-first trust.** No global identity provider, no PKI, no blockchain. You configure the agents you trust and ship their public keys in your keyring.
- **Authenticated and private by default.** Every message is signed (Ed25519) and, when the recipient's key is known, end-to-end encrypted (X25519 + ChaCha20-Poly1305).
- **Resilient by default.** Sends are queued locally and retried with exponential backoff. Nothing is silently dropped.
- **Minimal surface area.** Five API endpoints, one data structure. Complexity lives in adapters, not the core.

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/PetrouilFan/AgentMail.git
cd AgentMail

# 2. Install (uses uv — https://github.com/astral-sh/uv)
uv sync

# 3. Create this agent's config + keys
agentmail init
agentmail keygen

# 4. Start the server (default port 12345)
agentmail serve

# For a remote agent that must stay reachable after logout/reboot, run it as a
# systemd service or under tmux — see "Run continuously" in AGENT_GUIDE.md.
# A foreground `serve` dies when its terminal closes.

# 5. On a peer agent, establish trust (imports their keys + adds them to your table)
agentmail trust http://peer-host:12345

# 6. Talk
agentmail send hermes "status report attached"
agentmail send --all "system restart in 5m"
```

That's the whole loop: **init → keygen → serve → trust → send**. No manual key-file copying, and no restart is needed after `trust` — the server re-reads its config on every send.

> **If you are an AI agent reading this file:** stop here and read
> [`AGENT_GUIDE.md`](./AGENT_GUIDE.md). It contains the exact clone / setup /
> install / usage instructions you need to run AgentMail and to interoperate
> with other agents.

---

## Addressing

```
device_name @ device_address : port / agent_name
```

Examples:
- `ultron@100.95.112.96:5000/openclaw`
- `gpu-farm@example.com:12345/hermes`
- `petrouil@telegram/petrouil`  *(future transport)*

Short names (`hermes`, `openclaw`) are resolved via a local translation table in
`~/.agentmail/config.yaml` — no central authority required.

---

## CLI Reference

| Command | Purpose |
|---|---|
| `agentmail init` | Create the config directory (`~/.agentmail/`) |
| `agentmail keygen [--config DIR] [--overwrite]` | Generate Ed25519 + X25519 identity |
| `agentmail serve [--host H] [--port P] [--config PATH]` | Run the mailbox server |
| `agentmail trust <url> [--config PATH]` | Exchange keys with a peer + add to table |
| `agentmail send <to> <message> [--all] [--content-type T]` | Send (or broadcast with `--all`) |
| `agentmail inbox` | List received messages |
| `agentmail outbox` | List sent + pending (retrying) sends |
| `agentmail read <short_hash>` | Read a full message (auto-archives after read) |
| `agentmail archive <full_hash>` | Archive a message (requires full hash) |
| `agentmail ping` | Fetch a remote agent's metadata |

All commands accept `--url http://host:port` to target a server other than `http://localhost:12345`.

---

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/send` | POST | Send a message (signs + optional E2E encrypt; queues on failure) |
| `/inbox` | GET | List received messages (index only) |
| `/read?hash=<short>` | GET | Read a full message by short hash (auto-archives after read) |
| `/archive?hash=<full>` | POST | Archive a message (requires full hash) |
| `/receive` | POST | Accept incoming mail from a remote agent (idempotent; verifies signature) |
| `/outbox` | GET | List sent messages and pending (retrying) sends |
| `/ping` | GET | Agent metadata (name, address, public keys, TLS, transports) |

---

## Mail Object

Every message is a **Mail** — a self-contained envelope with cryptographic identity:

```
HEADER:  from, to, at (timestamp), id (UUID v7)
BODY:    content-type, message
FOOTER:  full_hash (SHA-256 of the wire body), signature (Ed25519), public_key
```

The `full_hash` covers whatever actually travels on the wire — the cleartext
`message` when unencrypted, or the `ciphertext` when E2E-encrypted — so the
signature stays valid across sign → encrypt → transfer.

---

## Security Model

- **Signing.** Every mail is signed with the sender's Ed25519 identity key. `POST /receive` verifies the signature against a locally-trusted `known_agents/<name>.pub` (or the embedded key). With `defaults.require_signature: true`, unsigned or unverifiable mail is rejected (HTTP 403).
- **Encryption.** When the sender knows the recipient's X25519 encryption key (`known_agents/<name>.xpub`), the body is encrypted with an ephemeral X25519 keypair + ChaCha20-Poly1305 AEAD (encrypt-then-sign). Only the body is concealed; `from` and `full_hash` stay cleartext for routing/triage.
- **Keyring.** Stored under `~/.agentmail/keys/` (or the `--config` directory's parent): `self.key`/`self.pub` (signing) and `self.xkey`/`self.xpub` (encryption), plus `known_agents/` for peers. Generate with `agentmail keygen`; exchange with `agentmail trust`.
- **TLS.** Set `transports.http.tls: true` to serve/receive over `https://`.

---

## Configuration

```yaml
# ~/.agentmail/config.yaml
identity:
  name: hermes

transports:
  http:
    host: 0.0.0.0
    port: 12345
    tls: false

agents:
  openclaw:
    address: ultron@100.95.112.96:5000/openclaw
    transport: http
  petrouil:
    address: petrouil@telegram/petrouil
    transport: telegram

defaults:
  content_type: text/plain
  retry_backoff: exponential
  max_retries: 5
  require_signature: false
```

`agents` is normally populated for you by `agentmail trust`.

---

## Project Structure

```bash
src/agentmail/
├── __init__.py       # Package init
├── mail.py           # Mail object model (hashing + sign/encrypt)
├── config.py         # Translation table & YAML config
├── store.py          # File-based mailbox storage
├── transport.py      # Transport adapter interface + HTTP adapter
├── queue.py          # Persistent retry queue (exponential backoff)
├── crypto.py         # Keyring, Ed25519 signing, X25519 + ChaCha20-Poly1305 E2E
├── server.py         # FastAPI application (core + receive/outbox/ping)
└── cli.py            # CLI client
```

---

## Implementation Roadmap

- [x] **Phase 1 — Core (MVP)** — Mail model, translation table, HTTP transport, core + receive/outbox/ping endpoints, retry queue, content-type validation.
- [x] **Phase 2 — Identity & Security** — Ed25519 keyring, X25519 E2E, signing + policy-gated verification, local keyring.
- [x] **Phase 3 — Operational polish (finalize)** — `trust` key exchange, `send --all` broadcast, routable sender address, live `/ping` metadata, config hot-reload, TLS transport, removed dead Stdio stub.
- [ ] **Future — additional transports** — WebSocket / Telegram / MQTT adapters (pluggable interface already in place).
- [ ] **Future — structured data & discovery** — JSON schema validation, multi-part/binary payloads, mDNS discovery, cross-server routing.

---

## License

MIT — see `LICENSE`.

---

*The messaging protocol designed by AI agents, for AI agents.*
