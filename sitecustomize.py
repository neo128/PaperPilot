"""
Auto-load .env values into os.environ for local scripts.

Python automatically imports sitecustomize.py if it is on sys.path (the repo
root is on sys.path when you run `python scripts/...` from here), so this
keeps per-user secrets out of the code without needing `source` in the shell.
Existing environment variables take precedence over .env values.
"""
from __future__ import annotations

import os
from pathlib import Path


def _parse_env_line(line: str) -> tuple[str, str] | tuple[None, None]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None, None
    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    return key or None, value


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    try:
        content = dotenv_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    for line in content:
        key, value = _parse_env_line(line)
        if not key:
            continue
        if key in os.environ:
            continue  # do not override existing environment
        os.environ[key] = value


load_dotenv(Path(__file__).resolve().parent / ".env")
