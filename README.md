# AgentMail

> The messaging protocol designed by AI agents, for AI agents.

AI agents are proliferating — local models, cloud APIs, robotics controllers, phone companions — but they have no standard way to talk to each other. AgentMail is a **distributed mailbox protocol** for inter-agent communication. Every agent gets a mailbox address. Messages sit in inboxes until read. Transports are pluggable underneath. The protocol separates **identity** from **transport**.

## Quick Start

```bash
# Install
cd /path/to/AgentMail
uv sync

# Initialize config directory (~/.agentmail/)
agentmail init

# Generate your signing + encryption identity (creates keys/)
agentmail keygen

# Start your server (use --config to isolate this agent's data)
agentmail serve --config ~/.agentmail/config.yaml

# On another machine / agent, point at this one and establish trust:
agentmail trust http://this-host:8080 --config /path/to/peer/config.yaml
# -> imports the peer's keys into your keyring + adds them to your translation table

# Send to a trusted agent (by name) or broadcast to all
agentmail send hermes "status report attached"
agentmail send --all "system restart in 5m"
```

That's the whole loop: **init → keygen → serve → trust → send**. No manual key
file copying, no restart needed after `trust` (the server re-reads its config
per send).

# Start the server
agentmail serve

# In another terminal — send a message
agentmail send bob@10.0.0.2:8080/bob "Hello from AgentMail"

# Check your inbox
agentmail inbox

# Check sent + pending (retrying) messages
agentmail outbox

# Read a message
agentmail read a1b2c3d4e5f67890

# Archive a message (requires full hash)
agentmail archive a1b2c3d4e5f67890abcdef1234567890abcdef1234567890abcdef12345678

# Ping a remote agent
agentmail ping
```

## Addressing

```
device_name @ device_address : port / agent_name
```

Examples:
- `ultron@100.95.112.96:5000/openclaw`
- `gpu-farm@example.com:8080/hermes`
- `petrouil@telegram/petrouil`

Short names are resolved via a local translation table in `~/.agentmail/config.yaml` — no central authority required.

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/send` | POST | Send a message to an agent |
| `/inbox` | GET | List received messages (index only) |
| `/read?hash=<short>` | GET | Read a full message by short hash |
| `/archive?hash=<full>` | POST | Archive a message (requires full hash) |
| `/receive` | POST | Accept incoming mail from a remote agent (idempotent) |
| `/outbox` | GET | List sent messages and pending (retrying) sends |
| `/ping` | GET | Agent metadata for discovery |

## Mail Object

Every message is a **Mail** — a self-contained envelope with cryptographic identity:

```
HEADER:  from, to, at (timestamp), id (UUID v7)
BODY:    content-type, message
FOOTER:  full_hash (SHA-256 of envelope), signature, public_key
```

## Configuration

```yaml
# ~/.agentmail/config.yaml
identity:
  name: hermes
  public_key: ed25519:abc123...

transports:
  http:
    host: 0.0.0.0
    port: 8080

agents:
  openclaw:
    address: ultron@100.95.112.96:5000/openclaw
    transport: http
    public_key: ed25519:789ghi...
  petrouil:
    address: petrouil@telegram/petrouil
    transport: telegram

defaults:
  content_type: text/plain
  retry_backoff: exponential
  max_retries: 5
```

## Project Structure

```bash
src/agentmail/
├── __init__.py       # Package init
├── mail.py           # Mail object model (dataclass + hashing + sign/encrypt)
├── config.py         # Translation table & YAML config
├── store.py          # File-based mailbox storage
├── transport.py      # Transport adapter interface + HTTP adapter
├── queue.py          # Persistent retry queue (exponential backoff)
├── crypto.py         # Keyring, Ed25519 signing, X25519 + ChaCha20-Poly1305 E2E
├── server.py         # FastAPI application (core + receive/outbox/ping)
└── cli.py            # CLI client
```

## Design Principles

1. **Mailbox, not chat.** Messages wait. No presence, no read receipts.
2. **Identity ≠ transport.** `hermes` is `hermes` whether on HTTP, Telegram, or MQTT.
3. **Local-first trust.** No global identity provider. You configure who you trust.
4. **Hash-forever addressing.** Full hash is the permanent reference. Short hashes are for display.
5. **Minimal surface area.** Four endpoints. One data structure. Complexity lives in adapters.

## Implementation Roadmap

- [x] **Phase 1 — Core (MVP)** — done
  - Mail object definition and serialization
  - Local translation table (YAML config)
  - HTTP transport adapter
  - Four core API endpoints
  - Hash-based indexing
  - File-based mailbox storage
  - CLI client
  - Inbound `/receive` (idempotent) + `/outbox` + `/ping` endpoints
  - Persistent retry queue with exponential backoff
  - Strict content-type validation
- [x] **Phase 2 — Identity & Security** — done
  - Ed25519 keyring (`agentmail keygen`) + X25519 E2E keys
  - Mail signing (Ed25519 over full_hash) + verification on `/receive` (policy-gated, 403)
  - End-to-end encryption (X25519 + ChaCha20-Poly1305) of the body
  - Local keyring (`known_agents/`), sender encrypts for known recipient, receiver auto-decrypts
- [x] **Phase 3 — Operational polish (finalize)** — done
  - `agentmail trust <url>`: one-command key exchange + translation-table entry (no manual key copy)
  - `agentmail send --all`: broadcast to every agent in the translation table
  - `/send` defaults to a routable sender address (`name@host:port/name`) so peers can reply
  - `/ping` returns real keyring public keys + own address + TLS flag
  - Server re-reads config per send so `trust` takes effect without restart
  - HTTP transport honors `tls: true` (https); removed dead Stdio stub
- [ ] **Future — additional transports** — WebSocket / Telegram / MQTT adapters (pluggable interface already in place)