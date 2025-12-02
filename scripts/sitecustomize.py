"""
Fallback sitecustomize inside scripts/ to auto-load the repo-level .env when
running scripts directly (even if the repo root is not on sys.path).
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


REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")
