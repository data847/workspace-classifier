"""Recursive Google Drive folder listing (metadata)."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

_FOLDER_ID_IN_URL = re.compile(r"/folders/([a-zA-Z0-9_-]+)")

# Fields requested from the Drive API per file entry.
# Includes owners and lastModifyingUser for attribution columns in the XLSX.
_LIST_FIELDS = (
    "nextPageToken, files("
    "id, name, mimeType, modifiedTime, size, "
    "webViewLink, md5Checksum, shortcutDetails, driveId, "
    "owners(emailAddress), lastModifyingUser(emailAddress)"
    ")"
)

_LIST_PAGE_SIZE = 1000
_LIST_MAX_ATTEMPTS = 5
_LIST_RETRY_ERRORS = (
    "timed out",
    "timeout",
    "read operation",
    "connection reset",
    "remote end closed",
    "broken pipe",
    "503",
    "500",
    "429",
)


def normalize_folder_id(raw: str) -> str:
    """Accept a raw folder ID or a ``drive.google.com`` folder URL.

    ``my-drive`` / ``root`` and common "My Drive" URLs resolve to the API root
    folder id ``root``.
    """
    s = (raw or "").strip()
    low = s.lower()
    if low in ("root", "my-drive", "mydrive"):
        return "root"
    if "drive.google.com" in low and "my-drive" in low:
        return "root"
    m = _FOLDER_ID_IN_URL.search(s)
    if m:
        return m.group(1)
    return s.split("?")[0].split("/")[0]


def _is_retryable_list_error(e: Exception) -> bool:
    msg = str(e).lower()
    status = getattr(getattr(e, "resp", None), "status", None)
    return status in (429, 500, 503) or any(kw in msg for kw in _LIST_RETRY_ERRORS)


def _execute_with_retry(
    request,
    *,
    progress_log: Callable[[str], None] | None,
    context: str,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(_LIST_MAX_ATTEMPTS):
        try:
            return request.execute()
        except Exception as e:
            last_exc = e
            if not _is_retryable_list_error(e) or attempt >= _LIST_MAX_ATTEMPTS - 1:
                raise
            wait = 2 ** attempt
            if progress_log:
                progress_log(
                    f"[scan] transient Drive API error while listing {context}: {e} "
                    f"(attempt {attempt + 1}/{_LIST_MAX_ATTEMPTS}); retrying in {wait}s"
                )
            time.sleep(wait)
    raise last_exc or RuntimeError("Drive API request failed without an exception")


def _list_children(
    service,
    folder_id: str,
    *,
    progress_log: Callable[[str], None] | None = None,
    context: str = "",
) -> list[dict[str, Any]]:
    q = f"'{folder_id}' in parents and trashed = false"
    out: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        request = service.files().list(
            q=q,
            pageSize=_LIST_PAGE_SIZE,
            fields=_LIST_FIELDS,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        resp = _execute_with_retry(
            request,
            progress_log=progress_log,
            context=context or folder_id,
        )
        out.extend(resp.get("files") or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def _row_from_file(f: dict[str, Any], path: str, *, is_folder: bool, is_shortcut: bool) -> dict[str, Any]:
    """Build a scan row dict from a Drive API file object."""
    return {
        "drive_file_id": f.get("id"),
        "path": path,
        "name": f.get("name") or "",
        "mime_type": f.get("mimeType") or "",
        "modified_time": f.get("modifiedTime"),
        "size_bytes": f.get("size"),
        "web_view_link": f.get("webViewLink"),
        "md5_checksum": f.get("md5Checksum"),
        "is_folder": is_folder,
        "is_shortcut": is_shortcut,
        "shortcut_target_id": (f.get("shortcutDetails") or {}).get("targetId"),
        # Attribution: first owner email; empty string when API doesn't return it.
        "owner_email": ((f.get("owners") or [{}])[0] or {}).get("emailAddress") or "",
        "last_modified_by": (f.get("lastModifyingUser") or {}).get("emailAddress") or "",
    }


def walk_drive_folder(
    service,
    folder_id: str,
    *,
    path_prefix: str = "",
    include_folders: bool = False,
    max_files: int | None = None,
    progress_log: Callable[[str], None] | None = None,
    progress_every: int = 500,
    scan_cache_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Depth-first walk from ``folder_id``; return flat rows (files and optionally folders).

    Each row: ``drive_file_id``, ``path``, ``name``, ``mime_type``, ``modified_time``,
    ``size_bytes``, ``web_view_link``, ``md5_checksum``, ``is_folder``, ``is_shortcut``,
    ``owner_email``, ``last_modified_by``.

    scan_cache_path
        If set, the complete walk result is saved to this JSONL file after the walk
        completes. On subsequent calls with the same path, the cache is loaded and
        the Drive API walk is skipped entirely — useful for large drives where the walk
        takes minutes and the downstream pipeline is what crashes/restarts.
        Delete the cache file to force a fresh walk.

    If ``max_files`` is set, stop after that many file rows (non-folder rows, including
    shortcuts). Folder rows from ``include_folders`` do not count toward the limit.

    If ``progress_log`` is set, it is called every ``progress_every`` **file** rows
    (shortcuts and regular files; not folder-only rows) with a short status line.
    """
    # --- cache load ---
    cache_path: Path | None = Path(scan_cache_path) if scan_cache_path else None
    if cache_path is not None and cache_path.is_file() and cache_path.stat().st_size > 0:
        rows: list[dict[str, Any]] = []
        with cache_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
        if rows:
            if progress_log:
                progress_log(f"[scan_cache] loaded {len(rows)} rows from {cache_path}")
            if max_files is not None:
                file_rows = [r for r in rows if not r.get("is_folder")]
                folder_rows = [r for r in rows if r.get("is_folder")]
                rows = (folder_rows if include_folders else []) + file_rows[:max_files]
            return rows

    # --- live walk ---
    folder_id = normalize_folder_id(folder_id)
    rows = []
    file_rows = 0
    stop = False

    def _emit_progress(path_hint: str) -> None:
        if progress_log and file_rows > 0 and file_rows % progress_every == 0:
            progress_log(f"files={file_rows} last={path_hint[:240]}")

    def visit(fid: str, rel_path: str) -> None:
        nonlocal file_rows, stop
        if stop:
            return
        children = _list_children(service, fid, progress_log=progress_log, context=rel_path or "root")
        # Stable order: folders first, then files, by name
        children.sort(
            key=lambda f: (0 if f.get("mimeType") == FOLDER_MIME else 1, (f.get("name") or "").lower())
        )
        for f in children:
            if stop:
                break
            name = f.get("name") or ""
            mid = f.get("mimeType") or ""
            sub = f"{rel_path}/{name}".strip("/") if rel_path else name
            is_folder = mid == FOLDER_MIME
            is_shortcut = mid == SHORTCUT_MIME

            if is_shortcut:
                rows.append(_row_from_file(f, sub, is_folder=False, is_shortcut=True))
                file_rows += 1
                _emit_progress(sub)
                if max_files is not None and file_rows >= max_files:
                    stop = True
                continue

            if is_folder:
                if include_folders:
                    rows.append(_row_from_file(f, sub, is_folder=True, is_shortcut=False))
                visit(f["id"], sub)
                continue

            rows.append(_row_from_file(f, sub, is_folder=False, is_shortcut=False))
            file_rows += 1
            _emit_progress(sub)
            if max_files is not None and file_rows >= max_files:
                stop = True
                return

    visit(folder_id, path_prefix)

    # --- cache save (only when walk completed without a max_files cap, so the cache is complete) ---
    if cache_path is not None and not stop:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp_path.replace(cache_path)
        if progress_log:
            progress_log(f"[scan_cache] saved {len(rows)} rows to {cache_path}")

    return rows


def list_shared_drives(service) -> list[dict[str, Any]]:
    """Return all Shared Drives (Team Drives) the authenticated user can access."""
    drives: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        resp = (
            service.drives()
            .list(pageSize=100, fields="nextPageToken, drives(id, name)", pageToken=page_token)
            .execute()
        )
        drives.extend(resp.get("drives") or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return drives


def walk_entire_workspace(
    service,
    *,
    include_my_drive: bool = True,
    include_shared_drives: bool = True,
    max_files: int | None = None,
    progress_log: Callable[[str], None] | None = None,
    progress_every: int = 500,
    scan_cache_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Walk the full Google Workspace: My Drive + every Shared Drive the user can access.

    Each file row's ``path`` is prefixed with the drive name so sources are
    distinguishable in the inventory (e.g. ``My Drive/Finance/Q3.xlsx`` vs
    ``Engineering/repo-docs/README.md``).
    """
    all_rows: list[dict[str, Any]] = []
    remaining = max_files

    if include_my_drive:
        if progress_log:
            progress_log("[workspace] walking My Drive ...")
        rows = walk_drive_folder(
            service,
            "root",
            path_prefix="My Drive",
            max_files=remaining,
            progress_log=progress_log,
            progress_every=progress_every,
            scan_cache_path=(
                str(scan_cache_path) + ".mydrive.jsonl" if scan_cache_path else None
            ),
        )
        all_rows.extend(rows)
        if remaining is not None:
            remaining = max(0, remaining - len(rows))
            if remaining == 0:
                return all_rows

    if include_shared_drives:
        shared = list_shared_drives(service)
        if progress_log:
            progress_log(f"[workspace] found {len(shared)} Shared Drive(s)")
        for drv in shared:
            did = drv["id"]
            dname = drv.get("name") or did
            if progress_log:
                progress_log(f"[workspace] walking Shared Drive: {dname!r}")
            rows = walk_drive_folder(
                service,
                did,
                path_prefix=dname,
                max_files=remaining,
                progress_log=progress_log,
                progress_every=progress_every,
                scan_cache_path=(
                    str(scan_cache_path) + f".{did}.jsonl" if scan_cache_path else None
                ),
            )
            all_rows.extend(rows)
            if remaining is not None:
                remaining = max(0, remaining - len(rows))
                if remaining == 0:
                    break

    return all_rows
