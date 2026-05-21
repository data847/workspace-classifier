from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


def checkpoint_path_for_output(output_path: str) -> Path:
    out = Path(output_path).expanduser().resolve()
    return out.with_suffix(out.suffix + ".checkpoint.jsonl")


def load_checkpoint(path: Path) -> dict[int, dict[str, Any]]:
    if not path.exists():
        return {}
    by_id: dict[int, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                _log.debug("Skipping malformed checkpoint line: %r", line[:80])
                continue
            rid = obj.get("row_id")
            if rid is None:
                continue
            try:
                rid_i = int(rid)
            except Exception:
                _log.debug("Skipping checkpoint entry with non-integer row_id: %r", rid)
                continue
            by_id[rid_i] = obj
    return by_id


def append_checkpoint(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file in the same directory, then atomically rename-append.
    # This prevents a corrupt final line if the process crashes mid-write.
    lines = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(lines)
        # Append the temp file contents to the checkpoint, then remove temp.
        with path.open("a", encoding="utf-8") as dst, open(tmp, "r", encoding="utf-8") as src:
            dst.write(src.read())
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def drive_artifact_checkpoint_path(output_path: str) -> Path:
    """Sidecar for Drive ingest: extracted evidence rows (pre-bucket), for resume without re-download."""
    out = Path(output_path).expanduser().resolve()
    return out.with_name(out.stem + ".drive_artifacts.jsonl")


def load_drive_artifact_checkpoint(path: Path) -> dict[int, dict[str, Any]]:
    """Map artifact_id -> evidence dict (classification columns may be empty)."""
    if not path.exists():
        return {}
    by_id: dict[int, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                _log.debug("Skipping malformed drive artifact checkpoint line: %r", line[:80])
                continue
            aid = obj.get("artifact_id")
            row = obj.get("row")
            if aid is None or not isinstance(row, dict):
                continue
            try:
                aid_i = int(aid)
            except Exception:
                _log.debug("Skipping drive artifact checkpoint entry with non-integer artifact_id: %r", aid)
                continue
            by_id[aid_i] = row
    return by_id


def append_drive_artifact_checkpoint(path: Path, artifact_id: int, row: dict[str, Any]) -> None:
    append_checkpoint(path, [{"artifact_id": artifact_id, "row": row}])

