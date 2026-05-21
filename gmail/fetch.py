"""Gmail export for a single user via DWD.

Exports:
  - emails_all.json       — all messages in one file (metadata + full ``body`` text each)
  - emails_metadata.csv   — id, subject, sender, date, has_attachments, snippet
  - email_<n>.txt         — full plain-text body of each email
  - attachments/<msg_id>/ — raw attachment files

All paths are written under a caller-supplied output directory.
"""

from __future__ import annotations

import base64
import csv
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError


# ---------------------------------------------------------------------------
# Gmail service builder (imported from gdrive.credentials to share SA)
# ---------------------------------------------------------------------------

def build_gmail_service(user_email: str, sa_file: Path | str | None = None):
    """Return a Gmail API v1 service impersonating ``user_email`` via DWD."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    _GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

    sa = Path(sa_file).expanduser().resolve() if sa_file else _default_sa()
    creds = service_account.Credentials.from_service_account_file(
        str(sa), scopes=_GMAIL_SCOPES
    )
    return build("gmail", "v1", credentials=creds.with_subject(user_email), cache_discovery=False)


def _default_sa() -> Path:
    from gdrive.credentials import default_sa_path
    return default_sa_path()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _backoff(attempt: int) -> None:
    base = 1.0 * (2 ** attempt)
    time.sleep(min(60.0, base))


def _safe_filename(name: str, max_len: int = 180) -> str:
    """Sanitise a string for use as a filename."""
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip(". ")
    return name[:max_len] or "unnamed"


def _decode_body(part: dict[str, Any]) -> str:
    """Decode a message part body to a UTF-8 string (best-effort)."""
    data = (part.get("body") or {}).get("data", "")
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "==")
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_text_and_attachments(
    payload: dict[str, Any],
    *,
    prefer_html: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """Recursively extract plain-text body and attachment metadata from a message payload."""
    body_parts: list[str] = []
    attachments: list[dict[str, Any]] = []
    mime = payload.get("mimeType", "")

    if mime == "text/plain" and not prefer_html:
        body_parts.append(_decode_body(payload))
    elif mime == "text/html" and prefer_html:
        body_parts.append(_decode_body(payload))
    elif mime.startswith("multipart/"):
        for sub in payload.get("parts") or []:
            sub_text, sub_att = _extract_text_and_attachments(sub, prefer_html=prefer_html)
            body_parts.append(sub_text)
            attachments.extend(sub_att)
    else:
        # Attachment part
        att_id = (payload.get("body") or {}).get("attachmentId")
        if att_id:
            attachments.append({
                "attachment_id": att_id,
                "filename": payload.get("filename") or "attachment",
                "mime_type": mime,
                "size": (payload.get("body") or {}).get("size", 0),
            })

    text = "\n".join(p for p in body_parts if p).strip()
    # If no plain text found, try HTML parts
    if not text and not prefer_html:
        text, _ = _extract_text_and_attachments(payload, prefer_html=True)

    return text, attachments


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_message_ids(
    service: Any,
    *,
    modified_after: datetime | None = None,
    max_results: int | None = None,
) -> list[str]:
    """Return Gmail message IDs, optionally filtered by date."""
    q = ""
    if modified_after is not None:
        if modified_after.tzinfo is None:
            modified_after = modified_after.replace(tzinfo=timezone.utc)
        # Gmail query uses after:YYYY/MM/DD
        q = f"after:{modified_after.strftime('%Y/%m/%d')}"

    ids: list[str] = []
    page_token: str | None = None

    while True:
        kwargs: dict[str, Any] = {"userId": "me", "maxResults": 500}
        if q:
            kwargs["q"] = q
        if page_token:
            kwargs["pageToken"] = page_token

        for attempt in range(5):
            try:
                resp = service.users().messages().list(**kwargs).execute()
                break
            except HttpError as e:
                if e.resp.status in (429, 500, 503) and attempt < 4:
                    _backoff(attempt)
                else:
                    raise

        ids.extend(m["id"] for m in resp.get("messages") or [])
        page_token = resp.get("nextPageToken")

        if not page_token:
            break
        if max_results is not None and len(ids) >= max_results:
            break

    return ids[:max_results] if max_results else ids


def fetch_and_export_emails(
    service: Any,
    *,
    out_dir: Path,
    modified_after: datetime | None = None,
    max_emails: int | None = None,
    log: Any = None,
    mailbox: str | None = None,
) -> int:
    """Fetch all emails for a user and export them to ``out_dir``.

    Writes:
      out_dir/emails_all.json      (``messages`` array with full ``body`` per message)
      out_dir/emails_metadata.csv  (includes ``mailbox`` column when ``mailbox`` is set)
      out_dir/mailbox.txt          (primary address, when ``mailbox`` is set)
      out_dir/email_<n>.txt        (leading ``Mailbox:`` line when ``mailbox`` is set)
      out_dir/attachments/<msg_id>/<filename>

    Returns the number of emails exported.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    att_root = out_dir / "attachments"
    if mailbox:
        (out_dir / "mailbox.txt").write_text(
            mailbox.strip() + "\n", encoding="utf-8"
        )

    def _log(msg: str) -> None:
        if log:
            log(msg)

    _log(f"[gmail] listing messages" + (f" after {modified_after.date()}" if modified_after else ""))
    msg_ids = list_message_ids(service, modified_after=modified_after, max_results=max_emails)
    _log(f"[gmail] found {len(msg_ids)} messages")

    meta_rows: list[dict[str, Any]] = []
    json_messages: list[dict[str, Any]] = []

    for idx, msg_id in enumerate(msg_ids, 1):
        if idx % 50 == 0:
            _log(f"[gmail] exported {idx}/{len(msg_ids)}")

        for attempt in range(5):
            try:
                msg = service.users().messages().get(
                    userId="me", id=msg_id, format="full"
                ).execute()
                break
            except HttpError as e:
                if e.resp.status in (429, 500, 503) and attempt < 4:
                    _backoff(attempt)
                else:
                    _log(f"[gmail] warn: could not fetch message {msg_id}: {e}")
                    msg = None
                    break

        if msg is None:
            continue

        payload  = msg.get("payload") or {}
        headers  = payload.get("headers") or []
        subject  = _header(headers, "Subject") or "(no subject)"
        sender   = _header(headers, "From") or ""
        date_str = _header(headers, "Date") or ""
        snippet  = msg.get("snippet") or ""

        body_text, attachments = _extract_text_and_attachments(payload)

        # Write email body
        email_file = out_dir / f"email_{idx:05d}.txt"
        mailbox_line = f"Mailbox: {mailbox}\n" if mailbox else ""
        email_content = (
            f"{mailbox_line}"
            f"From: {sender}\n"
            f"Date: {date_str}\n"
            f"Subject: {subject}\n"
            f"Message-ID: {msg_id}\n"
            f"{'=' * 60}\n\n"
            f"{body_text}"
        )
        email_file.write_text(email_content, encoding="utf-8", errors="replace")

        # Download attachments
        att_dir = att_root / msg_id
        downloaded_attachments: list[str] = []
        for att in attachments:
            att_id = att["attachment_id"]
            raw_name = att["filename"] or "attachment"
            safe_name = _safe_filename(raw_name)
            for attempt in range(4):
                try:
                    att_resp = service.users().messages().attachments().get(
                        userId="me", messageId=msg_id, id=att_id
                    ).execute()
                    att_data = base64.urlsafe_b64decode(att_resp.get("data", "") + "==")
                    att_dir.mkdir(parents=True, exist_ok=True)
                    (att_dir / safe_name).write_bytes(att_data)
                    downloaded_attachments.append(safe_name)
                    break
                except HttpError as e:
                    if e.resp.status in (429, 500, 503) and attempt < 3:
                        _backoff(attempt)
                    else:
                        _log(f"[gmail] warn: attachment download failed {msg_id}/{raw_name}: {e}")
                        break

        row: dict[str, Any] = {
            "message_id":    msg_id,
            "index":         idx,
            "subject":       subject,
            "sender":        sender,
            "date":          date_str,
            "snippet":       snippet[:300],
            "body_file":     email_file.name,
            "has_attachments": len(attachments) > 0,
            "attachments":   "; ".join(downloaded_attachments),
        }
        if mailbox:
            row = {"mailbox": mailbox, **row}
        meta_rows.append(row)

        att_rel: list[dict[str, str]] = []
        for name in downloaded_attachments:
            rel = str(Path("attachments") / msg_id / name).replace("\\", "/")
            att_rel.append({"filename": name, "relative_path": rel})

        jm: dict[str, Any] = {
            "message_id": msg_id,
            "index": idx,
            "subject": subject,
            "from": sender,
            "to": _header(headers, "To"),
            "cc": _header(headers, "Cc"),
            "date": date_str,
            "snippet": snippet,
            "body": body_text,
            "body_file": email_file.name,
            "has_attachments": len(attachments) > 0,
            "attachments": att_rel,
        }
        if mailbox:
            jm = {"mailbox": mailbox, **jm}
        json_messages.append(jm)

    json_path = out_dir / "emails_all.json"
    payload: dict[str, Any] = {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "message_count": len(json_messages),
        "messages": json_messages,
    }
    if mailbox:
        payload["mailbox"] = mailbox.strip()
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Write metadata CSV
    csv_path = out_dir / "emails_metadata.csv"
    if meta_rows:
        fieldnames = list(meta_rows[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(meta_rows)

    _log(f"[gmail] export complete: {len(meta_rows)} emails → {out_dir} (emails_all.json)")
    return len(meta_rows)
