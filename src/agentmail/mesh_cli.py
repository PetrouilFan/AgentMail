"""Mesh roster export — the importable function behind `agentmail mesh export`.

Kept as a package module so the CLI command and the standalone `tools/gen_mesh.py`
both delegate here. Exports PUBLIC keys only (signing + encryption) for every
known agent into a portable roster file. Never includes private keys.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization

from .config import load_config
from .crypto import KeyRing


def _my_host(config) -> str:
    http = config.transports.get("http")
    if http and http.host not in ("0.0.0.0", ""):
        return http.host
    return "127.0.0.1"


def _my_port(config) -> int:
    http = config.transports.get("http")
    return http.port if http else 12345


def mesh_export(cfg_path: Path, out_path: Path) -> int:
    """Write every known agent's public keys + addresses to a mesh roster.

    Returns the number of agents written.
    """
    config = load_config(cfg_path)
    kr = KeyRing(base_dir=cfg_path.parent)

    agents: dict[str, dict] = {}

    # This agent (self-describing for new nodes).
    if kr.has_identity():
        agents[config.identity.name] = {
            "address": f"{config.identity.name}@{_my_host(config)}:{_my_port(config)}/{config.identity.name}",
            "transport": "http",
            "signing_key": kr.self_signing_public_pem().decode().strip(),
            "encryption_key": kr.self_enc_public_pem().decode().strip(),
        }

    # Known peers from the translation table.
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

    lines = [
        "# AgentMail mesh roster — public keys only, safe to share",
        "# Distributed out-of-band (Tailscale file share). Never contains private keys.",
        "agents:",
    ]
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
    return len(agents)
