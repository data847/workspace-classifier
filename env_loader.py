"""Load .env from the workspace-classifier project root."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

_ROOT = Path(__file__).resolve().parent
_ENV_PATH = _ROOT / ".env"
_OPERATOR_ENV_PATH = _ROOT / "operator.env"

load_dotenv(_ENV_PATH)
# Shared infrastructure creds (Hetzner SFTP, etc.) — same as drivetocloud / cloud_transfer.
load_dotenv(_OPERATOR_ENV_PATH, override=True)


def _fill_key(env_var: str) -> None:
    if (os.environ.get(env_var) or "").strip():
        return
    vals = dotenv_values(_ENV_PATH)
    if not vals:
        return
    key = (vals.get(env_var) or "").strip().strip('"').strip("'")
    if key:
        os.environ[env_var] = key


_fill_key("ANTHROPIC_API_KEY")
_fill_key("OPENAI_API_KEY")
