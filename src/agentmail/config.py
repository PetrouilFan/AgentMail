"""Translation table — local address resolution for agents.

Maps short client IDs to full addresses with transport preferences.
This is essentially /etc/hosts for agents — local, editable, no central authority.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel


class AgentEntry(BaseModel):
    """A single entry in the translation table."""

    address: str
    transport: str = "http"
    public_key: str = ""


class DefaultsConfig(BaseModel):
    """Default settings for the AgentMail server."""

    content_type: str = "text/plain"
    retry_backoff: str = "exponential"
    max_retries: int = 5
    archive_after_days: Optional[int] = None
    require_signature: bool = False


class IdentityConfig(BaseModel):
    """This agent's identity configuration."""

    name: str = "agent"
    public_key: str = ""


class TransportConfig(BaseModel):
    """Transport-specific configuration."""
    host: str = "0.0.0.0"
    port: int = 12345
    token: str = ""
    router: bool = False
    tls: bool = False


def save_config(config: AgentMailConfig, path: Optional[Path] = None) -> Path:
    """Persist a config back to YAML.

    Used by the trust command to record newly-discovered peers.
    """
    path = path or DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_str = yaml.dump(config.model_dump(), default_flow_style=False, sort_keys=False)
    path.write_text(yaml_str)
    return path


class AgentMailConfig(BaseModel):
    """Full AgentMail configuration loaded from config.yaml."""

    identity: IdentityConfig = IdentityConfig()
    transports: dict[str, TransportConfig] = {}
    agents: dict[str, AgentEntry] = {}
    defaults: DefaultsConfig = DefaultsConfig()


# Default config directory
DEFAULT_CONFIG_DIR = Path.home() / ".agentmail"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.yaml"


def _expand_env(value: str) -> str:
    """Expand environment variables in string values like ${VAR}."""
    return os.path.expandvars(value)


def load_config(path: Optional[Path] = None) -> AgentMailConfig:
    """Load configuration from a YAML file.

    If the file doesn't exist, returns a blank config with defaults.
    Environment variables like ${TELEGRAM_BOT_TOKEN} are expanded.
    """
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return AgentMailConfig()

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    # Expand env vars in string values
    def _expand_dict(d: dict) -> dict:
        result = {}
        for k, v in d.items():
            if isinstance(v, str):
                result[k] = _expand_env(v)
            elif isinstance(v, dict):
                result[k] = _expand_dict(v)
            else:
                result[k] = v
        return result

    raw = _expand_dict(raw)
    return AgentMailConfig(**raw)


def resolve_address(client_id: str, config: AgentMailConfig) -> Optional[AgentEntry]:
    """Look up a client ID in the translation table.

    Returns the AgentEntry with full address and transport, or None.
    """
    return config.agents.get(client_id)


def init_config_dir(path: Optional[Path] = None) -> Path:
    """Create the default AgentMail directory structure if it doesn't exist.

    Returns the config directory path.
    """
    config_dir = path or DEFAULT_CONFIG_DIR
    (config_dir / "keys" / "known_agents").mkdir(parents=True, exist_ok=True)
    (config_dir / "inbox").mkdir(exist_ok=True)
    (config_dir / "outbox").mkdir(exist_ok=True)
    (config_dir / "archive").mkdir(exist_ok=True)
    (config_dir / "logs").mkdir(exist_ok=True)

    config_path = config_dir / "config.yaml"
    if not config_path.exists():
        sample = AgentMailConfig(identity=IdentityConfig(name="myagent"))
        yaml_str = yaml.dump(sample.model_dump(), default_flow_style=False, sort_keys=False)
        config_path.write_text(yaml_str)

    return config_dir