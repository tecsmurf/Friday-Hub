"""
Friday Hub configuration loader.

Loads environment variables and YAML config from, in order:
  1. FRIDAY_CONFIG, when set
  2. ./friday.yaml in the current working directory
  3. friday.yaml beside the source checkout
  4. the packaged default config
"""

import os
from pathlib import Path

from dotenv import load_dotenv
import yaml

PACKAGE_DIR = Path(__file__).parent
SOURCE_ROOT = PACKAGE_DIR.parent
DEFAULT_CONFIG_PATH = PACKAGE_DIR / "defaults" / "friday.yaml"


def _load_env() -> None:
    """Load optional .env files without overriding real environment vars."""
    candidates: list[Path] = []

    explicit = os.environ.get("FRIDAY_ENV_FILE")
    if explicit:
        candidates.append(Path(explicit).expanduser())

    candidates.extend([
        Path.cwd() / ".env",
        SOURCE_ROOT / ".env",
    ])

    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve() if path.exists() else path
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.exists():
            load_dotenv(path, override=False)


def _resolve_env(value):
    """Resolve ${VAR:-default} patterns in YAML values."""
    if not isinstance(value, str) or "${" not in value:
        return value

    import re

    def _repl(match):
        var = match.group(1)
        default = match.group(2) if match.group(2) is not None else ""
        return os.environ.get(var, default)

    return re.sub(r"\$\{(\w+)(?::-([^}]*))?\}", _repl, value)


def _walk_resolve(obj):
    """Recursively resolve env vars in config."""
    if isinstance(obj, dict):
        return {k: _walk_resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_resolve(item) for item in obj]
    return _resolve_env(obj)


def _candidate_config_paths() -> list[Path]:
    candidates: list[Path] = []

    explicit = os.environ.get("FRIDAY_CONFIG")
    if explicit:
        candidates.append(Path(explicit).expanduser())

    candidates.extend([
        Path.cwd() / "friday.yaml",
        SOURCE_ROOT / "friday.yaml",
        DEFAULT_CONFIG_PATH,
    ])

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve() if path.exists() else path
        if resolved not in seen:
            unique.append(path)
            seen.add(resolved)
    return unique


def _find_config_path() -> Path:
    for path in _candidate_config_paths():
        if path.exists():
            return path
    raise FileNotFoundError("No Friday config found. Set FRIDAY_CONFIG or install package data.")


def _resolve_root_dir(config_path: Path) -> Path:
    """Choose where Friday stores local memory, logs, and version locks."""
    friday_home = os.environ.get("FRIDAY_HOME")
    if friday_home:
        return Path(friday_home).expanduser()

    if config_path == DEFAULT_CONFIG_PATH:
        return Path.home() / ".friday"

    return config_path.parent


def load_config(config_path: Path | None = None) -> dict:
    """Load and return the resolved configuration."""
    if config_path is None:
        _load_env()
        config_path = _find_config_path()
    with open(config_path, "r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}
    return _walk_resolve(raw)


_load_env()
CONFIG_PATH = _find_config_path()
ROOT_DIR = _resolve_root_dir(CONFIG_PATH)
config = load_config(CONFIG_PATH)
