"""Tests for AgentMail config loading and translation table."""

import os
import tempfile
from pathlib import Path

import yaml

from agentmail.config import (
    AgentMailConfig,
    AgentEntry,
    DefaultsConfig,
    IdentityConfig,
    TransportConfig,
    load_config,
    resolve_address,
    init_config_dir,
)


SAMPLE_CONFIG = {
    "identity": {"name": "hermes", "public_key": "ed25519:abc123"},
    "transports": {
        "http": {"host": "0.0.0.0", "port": 8080},
        "telegram": {"token": "${TELEGRAM_BOT_TOKEN}", "router": True},
    },
    "agents": {
        "openclaw": {
            "address": "ultron@100.95.112.96:5000/openclaw",
            "transport": "http",
            "public_key": "ed25519:789ghi",
        },
        "petrouil": {
            "address": "petrouil@telegram/petrouil",
            "transport": "telegram",
        },
    },
    "defaults": {
        "content_type": "text/plain",
        "retry_backoff": "exponential",
        "max_retries": 5,
    },
}


class TestConfigLoading:
    """Test YAML config loading and parsing."""

    def test_load_from_file(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(SAMPLE_CONFIG))
        config = load_config(config_file)
        assert config.identity.name == "hermes"
        assert config.identity.public_key == "ed25519:abc123"
        assert "openclaw" in config.agents
        assert config.agents["openclaw"].transport == "http"
        assert config.defaults.max_retries == 5

    def test_load_missing_file(self, tmp_path):
        config = load_config(tmp_path / "nonexistent.yaml")
        assert config.identity.name == "agent"  # default

    def test_env_var_expansion(self, tmp_path):
        os.environ["TEST_AGENTMAIL_TOKEN"] = "secret123"
        cfg = {
            "identity": {"name": "test"},
            "transports": {"telegram": {"token": "${TEST_AGENTMAIL_TOKEN}"}},
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(cfg))
        config = load_config(config_file)
        assert config.transports["telegram"].token == "secret123"
        del os.environ["TEST_AGENTMAIL_TOKEN"]

    def test_defaults(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"identity": {"name": "myagent"}}))
        config = load_config(config_file)
        assert config.defaults.content_type == "text/plain"
        assert config.defaults.archive_after_days is None


class TestTranslationTable:
    """Test address resolution via the translation table."""

    def test_resolve_known_agent(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(SAMPLE_CONFIG))
        config = load_config(config_file)

        entry = resolve_address("openclaw", config)
        assert entry is not None
        assert entry.address == "ultron@100.95.112.96:5000/openclaw"
        assert entry.transport == "http"

    def test_resolve_unknown_agent(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(SAMPLE_CONFIG))
        config = load_config(config_file)

        entry = resolve_address("unknown_agent", config)
        assert entry is None


class TestInitConfigDir:
    """Test config directory initialization."""

    def test_init_creates_dirs(self, tmp_path):
        config_dir = tmp_path / ".agentmail"
        result = init_config_dir(config_dir)
        assert (result / "keys" / "known_agents").is_dir()
        assert (result / "inbox").is_dir()
        assert (result / "outbox").is_dir()
        assert (result / "archive").is_dir()
        assert (result / "logs").is_dir()
        assert (result / "config.yaml").exists()

    def test_init_idempotent(self, tmp_path):
        config_dir = tmp_path / ".agentmail"
        init_config_dir(config_dir)
        init_config_dir(config_dir)  # Should not raise