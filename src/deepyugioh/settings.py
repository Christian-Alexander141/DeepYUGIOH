"""Persistent local settings: deepyugioh.toml in the project root (not versioned).

The file has the same format as deepyugioh.example.toml plus one switch:

    test_mode = true     # enables developer tools such as the test GUI

The CLI uses the generator configuration in this file whenever no --config is
given; the test GUI reads and writes it. The location can be changed with
DEEPYUGIOH_SETTINGS. DEEPYUGIOH_TEST_MODE=1/0 overrides `test_mode`.
"""

from __future__ import annotations

import json
import math
import os
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from deepyugioh import paths

if TYPE_CHECKING:
    from deepyugioh.generator.config import GeneratorConfig

SETTINGS_ENV = "DEEPYUGIOH_SETTINGS"
TEST_MODE_ENV = "DEEPYUGIOH_TEST_MODE"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def settings_path() -> Path:
    env = os.environ.get(SETTINGS_ENV)
    return Path(env) if env else paths.ROOT / "deepyugioh.toml"


def load(path: Path | None = None) -> dict:
    """Raw settings; {} if the file does not exist yet."""
    path = path or settings_path()
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def save(data: dict, path: Path | None = None) -> Path:
    """Write the settings atomically (a crash never leaves a half written file)."""
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_HEADER + dumps(data), encoding="utf-8")
    os.replace(tmp, path)
    return path


def test_mode(path: Path | None = None) -> bool:
    env = os.environ.get(TEST_MODE_ENV)
    if env is not None:
        value = env.strip().lower()
        if value in _TRUE:
            return True
        if value in _FALSE:
            return False
        raise ValueError(f"{TEST_MODE_ENV}={env!r}: expected 1/0, true/false or on/off")
    return bool(load(path).get("test_mode", False))


def set_test_mode(on: bool, path: Path | None = None) -> Path:
    data = load(path)
    data["test_mode"] = bool(on)
    return save(data, path)


def gui_theme(path: Path | None = None) -> str:
    """Colour scheme of the test GUI: 'dark' (default) or 'light'."""
    theme = load(path).get("gui", {}).get("theme", "dark")
    return theme if theme in ("dark", "light") else "dark"


def set_gui_theme(theme: str, path: Path | None = None) -> Path:
    if theme not in ("dark", "light"):
        raise ValueError(f"unknown GUI theme {theme!r}")
    data = load(path)
    data.setdefault("gui", {})["theme"] = theme
    return save(data, path)


def generator_config(path: Path | None = None) -> "GeneratorConfig":
    """The persistent generator configuration (defaults for every missing key)."""
    from deepyugioh.generator.config import GeneratorConfig

    return GeneratorConfig.from_dict(load(path))


def save_generator_config(cfg: "GeneratorConfig", path: Path | None = None) -> Path:
    """Replace the generator part of the settings; other keys (test_mode) are kept."""
    cfg.check()
    data = {k: v for k, v in load(path).items() if k not in _GENERATOR_KEYS}
    data.update(cfg.to_dict())
    return save(data, path)


_GENERATOR_KEYS = {"limits", "candidates", "opponent_boards", "workers", "validate"}
_HEADER = (
    "# Local DeepYUGIOH settings - written by `deepyugioh testmode` and the test GUI.\n"
    "# Same format as deepyugioh.example.toml; used by the CLI when no --config is given.\n\n"
)


# ---- minimal TOML writer (tomllib can only read) ---------------------------
def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError(f"cannot store {v} in TOML settings")
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # JSON string escapes are valid TOML
    if isinstance(v, (list, tuple)):
        if any(isinstance(x, dict) for x in v):
            return "[\n" + "".join(f"  {_value(x)},\n" for x in v) + "]"
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot store {type(v).__name__} in TOML settings")


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() and k.isascii() else json.dumps(k)


def dumps(data: dict, _prefix: str = "") -> str:
    """Serialise nested dicts: scalars and lists first, then one [table] per dict."""
    lines, tables = [], []
    for k, v in data.items():
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            lines.append(f"{_key(k)} = {_value(v)}")
    out = "\n".join(lines) + ("\n" if lines else "")
    for k, v in tables:
        name = f"{_prefix}{_key(k)}"
        has_values = any(not isinstance(x, dict) for x in v.values())
        body = dumps(v, name + ".")
        if has_values or not v:
            out += f"\n[{name}]\n{body}"
        else:
            out += body
    return out
