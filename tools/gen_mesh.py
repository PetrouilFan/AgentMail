#!/usr/bin/env python3
"""Generate mesh.yaml — the AgentMail fleet roster.

Exports every known agent's address + PUBLIC keys (signing + encryption) from
the live config + keyring into a portable roster file. Public keys only — never
private keys. The output is gitignored and meant for out-of-band distribution
(e.g. Tailscale file share) so a new node can `agentmail mesh import` it and
learn the whole mesh in one shot.

This is data-tooling, not an AgentMail package feature. Run on a schedule to
keep the roster current as nodes join/leave.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from cryptography.hazmat.primitives import serialization
from agentmail.config import load_config, DEFAULT_CONFIG_PATH
from agentmail.crypto import KeyRing


def main() -> None:
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("mesh.yaml")
    config = load_config(cfg_path)
    kr = KeyRing(base_dir=cfg_path.parent)

    agents: dict[str, dict] = {}

    # This agent (so the roster is self-describing for new nodes)
    if kr.has_identity():
        agents[config.identity.name] = {
            "address": f"{config.identity.name}@{_my_host(config)}:{_my_port(config)}/{config.identity.name}",
            "transport": "http",
            "signing_key": kr.self_signing_public_pem().decode().strip(),
            "encryption_key": kr.self_enc_public_pem().decode().strip(),
        }

    # Known peers from the translation table
    for name, entry in config.agents.items():
        signing = ""
        enc = ""
        try:
            kp = kr.known_signing_public(name)
            if kp is not None:
                signing = kp.public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                ).decode().strip()
        except Exception:
            signing = entry.public_key.strip() if entry.public_key else ""
        try:
            kp = kr.known_enc_public(name)
            if kp is not None:
                enc = kp.public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                ).decode().strip()
        except Exception:
            enc = ""
        agents[name] = {
            "address": entry.address,
            "transport": entry.transport or "http",
            "signing_key": signing,
            "encryption_key": enc,
        }

    # Emit YAML (public keys only)
    lines = ["# AgentMail mesh roster — public keys only, safe to share",
             "# Distributed out-of-band (Tailscale file share). Never contains private keys.",
             "agents:"]
    for name, a in agents.items():
        lines.append(f"  {name}:")
        lines.append(f"    address: {a['address']}")
        lines.append(f"    transport: {a['transport']}")
        lines.append(f"    signing_key: |")
        for l in a["signing_key"].splitlines():
            lines.append(f"      {l}")
        lines.append(f"    encryption_key: |")
        for l in a["encryption_key"].splitlines():
            lines.append(f"      {l}")
    out_path.write_text("\n".join(lines) + "\n")
    print(f"wrote {out_path} ({len(agents)} agents: {', '.join(agents)})")


def _my_host(config):
    http = config.transports.get("http")
    if http and http.host not in ("0.0.0.0", ""):
        return http.host
    return "100.106.178.69"  # pixelbeast Tailscale IP fallback


def _my_port(config):
    http = config.transports.get("http")
    return http.port if http else 12345


if __name__ == "__main__":
    main()
