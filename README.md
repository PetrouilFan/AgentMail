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

# Edit config to add agents
vim ~/.agentmail/config.yaml

# Start the server
agentmail serve

# In another terminal — send a message
agentmail send bob@10.0.0.2:8080/bob "Hello from AgentMail"

# Check your inbox
agentmail inbox

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
| `/receive` | POST | Accept incoming mail from a remote agent |
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

```
src/agentmail/
├── __init__.py       # Package init
├── mail.py           # Mail object model (dataclass + hashing)
├── config.py         # Translation table & YAML config
├── store.py          # File-based mailbox storage
├── transport.py      # Transport adapter interface + HTTP adapter
├── server.py         # FastAPI application (4 core endpoints)
└── cli.py            # CLI client
```

## Design Principles

1. **Mailbox, not chat.** Messages wait. No presence, no read receipts.
2. **Identity ≠ transport.** `hermes` is `hermes` whether on HTTP, Telegram, or MQTT.
3. **Local-first trust.** No global identity provider. You configure who you trust.
4. **Hash-forever addressing.** Full hash is the permanent reference. Short hashes are for display.
5. **Minimal surface area.** Four endpoints. One data structure. Complexity lives in adapters.

## Implementation Roadmap

- [x] **Phase 1 — Core (MVP)** ← You are here
  - Mail object definition and serialization
  - Local translation table (YAML config)
  - HTTP transport adapter
  - Four core API endpoints
  - Hash-based indexing
  - File-based mailbox storage
  - CLI client
- [ ] **Phase 2 — Identity & Security** — Ed25519 signing, X25519+ChaCha20 encryption, local keyring
- [ ] **Phase 3 — Multi-Transport** — WebSocket, Telegram, MQTT adapters
- [ ] **Phase 4 — Structured Data** — JSON schema validation, multi-part messages, binary payloads
- [ ] **Phase 5 — Discovery & Federation** — mDNS, /ping metadata, cross-server routing