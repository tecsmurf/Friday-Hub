"""Tests for friday.config — YAML and env loading."""

import os
from pathlib import Path

import yaml


class TestConfigResolver:
    """Test the ${VAR:-default} resolver and config loading."""

    def test_resolve_env_with_default(self):
        """${VAR:-default} should return default when VAR is unset."""
        from friday.config import _resolve_env

        # Unset var — should use default
        os.environ.pop("_FRIDAY_TEST_VAR", None)
        assert _resolve_env("${_FRIDAY_TEST_VAR:-fallback}") == "fallback"

    def test_resolve_env_with_value(self):
        """${VAR:-default} should return env value when VAR is set."""
        from friday.config import _resolve_env

        os.environ["_FRIDAY_TEST_VAR"] = "real_value"
        try:
            assert _resolve_env("${_FRIDAY_TEST_VAR:-fallback}") == "real_value"
        finally:
            os.environ.pop("_FRIDAY_TEST_VAR", None)

    def test_resolve_env_no_pattern(self):
        """Plain strings should pass through unchanged."""
        from friday.config import _resolve_env

        assert _resolve_env("just a string") == "just a string"
        assert _resolve_env(42) == 42
        assert _resolve_env(None) is None

    def test_walk_resolve_nested(self):
        """_walk_resolve should handle nested dicts and lists."""
        from friday.config import _walk_resolve

        os.environ.pop("_FRIDAY_WALK_TEST", None)
        data = {
            "key": "${_FRIDAY_WALK_TEST:-default_val}",
            "nested": {
                "inner": "${_FRIDAY_WALK_TEST:-inner_default}",
            },
            "list": ["${_FRIDAY_WALK_TEST:-list_default}", "plain"],
        }
        resolved = _walk_resolve(data)
        assert resolved["key"] == "default_val"
        assert resolved["nested"]["inner"] == "inner_default"
        assert resolved["list"][0] == "list_default"
        assert resolved["list"][1] == "plain"


class TestConfigLoading:
    """Test config file loading."""

    def test_load_config_from_file(self, tmp_path):
        """load_config should read a YAML file and resolve env vars."""
        from friday.config import load_config

        config_file = tmp_path / "test_config.yaml"
        config_file.write_text(
            yaml.dump({
                "models": {
                    "primary_provider": "ollama",
                    "brain": {"ollama": "test-model:latest"},
                },
                "agents": {},
            }),
            encoding="utf-8",
        )

        cfg = load_config(config_file)
        assert cfg["models"]["primary_provider"] == "ollama"
        assert cfg["models"]["brain"]["ollama"] == "test-model:latest"

    def test_load_config_empty_file(self, tmp_path):
        """load_config should handle empty YAML files gracefully."""
        from friday.config import load_config

        config_file = tmp_path / "empty.yaml"
        config_file.write_text("", encoding="utf-8")

        cfg = load_config(config_file)
        assert cfg == {}

    def test_global_config_loaded(self):
        """The module-level `config` should be a populated dict."""
        from friday.config import config

        assert isinstance(config, dict)
        assert "models" in config
        assert "agents" in config


class TestConfigPaths:
    """Test config path resolution."""

    def test_root_dir_is_path(self):
        """ROOT_DIR should be a Path object."""
        from friday.config import ROOT_DIR

        assert isinstance(ROOT_DIR, Path)

    def test_config_path_exists(self):
        """CONFIG_PATH should point to an existing file."""
        from friday.config import CONFIG_PATH

        assert CONFIG_PATH.exists()
