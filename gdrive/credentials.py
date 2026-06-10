"""Google Drive API credentials.

Supports two auth modes:
  1. OAuth 2.0 (single user, browser-based) — original behaviour, unchanged.
  2. Service Account + Domain-Wide Delegation (DWD) — org-wide access.
     Set GOOGLE_SERVICE_ACCOUNT_FILE to your SA JSON key path, or place
     service_account.json next to this file / in the project root.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Least privilege for listing tree; use drive.readonly if you need file download later.
SCOPES_METADATA = ("https://www.googleapis.com/auth/drive.metadata.readonly",)
SCOPES_READONLY = ("https://www.googleapis.com/auth/drive.readonly",)

# DWD scopes
_DWD_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_DWD_ADMIN_SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
    "https://www.googleapis.com/auth/admin.reports.usage.readonly",
]


# ---------------------------------------------------------------------------
# Service Account path resolution
# ---------------------------------------------------------------------------

def default_sa_path() -> Path:
    """Resolve the service account JSON key file path.

    Search order:
      1. GOOGLE_SERVICE_ACCOUNT_FILE env var
      2. service_account.json in the project root (two levels up from this file)
      3. ../workspace-tool/service_account.json (sibling project — single source of truth)
    """
    env = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    project_root = Path(__file__).resolve().parents[1]
    local = project_root / "service_account.json"
    if local.is_file():
        return local
    sibling = project_root.parent / "workspace-tool" / "service_account.json"
    if sibling.is_file():
        return sibling
    return local  # will raise a clear error at use time


# ---------------------------------------------------------------------------
# DWD helpers
# ---------------------------------------------------------------------------

def get_dwd_credentials(
    user_email: str,
    *,
    sa_file: Path | str | None = None,
    scopes: list[str] | None = None,
) -> service_account.Credentials:
    """Return service-account credentials impersonating ``user_email`` via DWD."""
    sa = Path(sa_file).expanduser().resolve() if sa_file else default_sa_path()
    if not sa.is_file():
        raise FileNotFoundError(
            f"Service account key not found: {sa}\n"
            "Set GOOGLE_SERVICE_ACCOUNT_FILE env var or place service_account.json "
            "in the project root."
        )
    creds = service_account.Credentials.from_service_account_file(
        str(sa), scopes=scopes or _DWD_DRIVE_SCOPES
    )
    return creds.with_subject(user_email)


def build_drive_service_dwd(
    user_email: str,
    *,
    sa_file: Path | str | None = None,
    timeout: int = 120,
):
    """Return a Drive API v3 service impersonating ``user_email`` via DWD.

    ``timeout`` is the per-request read/connect timeout in seconds (default 120).
    Pass a higher value for large mailboxes that are slow to respond.
    """
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp

    creds = get_dwd_credentials(user_email, sa_file=sa_file, scopes=_DWD_DRIVE_SCOPES)
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout))
    return build("drive", "v3", http=http, cache_discovery=False)


def build_admin_service(
    admin_email: str,
    *,
    sa_file: Path | str | None = None,
):
    """Return an Admin SDK directory_v1 service impersonating ``admin_email`` via DWD."""
    creds = get_dwd_credentials(admin_email, sa_file=sa_file, scopes=_DWD_ADMIN_SCOPES)
    return build("admin", "directory_v1", credentials=creds, cache_discovery=False)


def list_org_users(admin_email: str, *, sa_file: Path | str | None = None) -> list[dict]:
    """Return all active users in the Workspace org via Admin SDK Directory API."""
    svc = build_admin_service(admin_email, sa_file=sa_file)
    users: list[dict] = []
    page_token = None
    while True:
        resp = svc.users().list(
            customer="my_customer",
            maxResults=500,
            orderBy="email",
            query="isSuspended=false",
            pageToken=page_token,
        ).execute()
        for u in resp.get("users", []):
            users.append({
                "email":    u.get("primaryEmail", ""),
                "name":     u.get("name", {}).get("fullName", ""),
                "org_unit": u.get("orgUnitPath", "/"),
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return users


def get_org_user(
    user_email: str,
    admin_email: str,
    *,
    sa_file: Path | str | None = None,
) -> dict:
    """Return metadata for a single active Workspace user via Admin SDK."""
    from googleapiclient.errors import HttpError

    svc = build_admin_service(admin_email, sa_file=sa_file)
    try:
        u = svc.users().get(userKey=user_email).execute()
    except HttpError as e:
        raise ValueError(f"User not found or not accessible: {user_email}") from e
    if u.get("suspended"):
        raise ValueError(f"User is suspended: {user_email}")
    return {
        "email":    u.get("primaryEmail", user_email),
        "name":     u.get("name", {}).get("fullName", ""),
        "org_unit": u.get("orgUnitPath", "/"),
    }


def default_client_secrets_path() -> Path:
    """Prefer ``GOOGLE_OAUTH_CLIENT_SECRETS``; otherwise ``.secrets/google_oauth_client.json``.

    This code never moves or renames your JSON; put the file wherever you want and
    point the env var at it if not using the default path.
    """
    p = (os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS") or "").strip()
    if p:
        return Path(p).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / ".secrets" / "google_oauth_client.json"


def default_token_path() -> Path:
    p = (os.environ.get("GOOGLE_OAUTH_TOKEN_PATH") or "").strip()
    if p:
        return Path(p).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / ".secrets" / "google_drive_token.json"


def _run_local_server_port(client_secrets: Path) -> int:
    """Port for ``run_local_server``.

    **Web** OAuth clients require an exact redirect URI match in Cloud Console.
    A random port (``0``) breaks that, so we default to **8080** unless
    ``GOOGLE_OAUTH_LOCAL_PORT`` is set. **Desktop / installed** clients use
    ``0`` (OS-assigned) unless the env var overrides.
    """
    env = (os.environ.get("GOOGLE_OAUTH_LOCAL_PORT") or "").strip()
    if env:
        return int(env)
    with open(client_secrets, encoding="utf-8") as f:
        data = json.load(f)
    if "web" in data:
        return 8080
    return 0


def get_credentials(
    *,
    client_secrets: Path | None = None,
    token_path: Path | None = None,
    full_read_scope: bool = False,
    login_only: bool = False,
    open_browser: bool | None = None,
) -> Credentials:
    """Load or obtain user OAuth credentials and persist ``token_path``.

    If there is no valid token, runs the local redirect OAuth flow. By default the
    library opens a **new** browser tab to Google's consent URL (not the same tab
    as drive.google.com). Set ``open_browser=False`` or env ``GOOGLE_OAUTH_OPEN_BROWSER=0``
    to only print the URL so you can paste it into an existing window.
    """
    client_secrets = client_secrets or default_client_secrets_path()
    token_path = token_path or default_token_path()
    scopes = SCOPES_READONLY if full_read_scope else SCOPES_METADATA
    scope_set = set(scopes)

    if not client_secrets.is_file():
        raise FileNotFoundError(
            f"OAuth client secrets JSON not found: {client_secrets}\n"
            "Download an OAuth client JSON from Google Cloud Console (Desktop app recommended) "
            "and save it there, or set GOOGLE_OAUTH_CLIENT_SECRETS to its path."
        )

    # If we need drive.readonly but the saved token was minted with metadata-only scope,
    # refresh will fail with invalid_scope — delete so we run a fresh consent flow.
    if token_path.is_file() and full_read_scope:
        try:
            data = json.loads(token_path.read_text(encoding="utf-8"))
            have = data.get("scopes")
            if isinstance(have, list):
                if not scope_set.issubset(set(have)):
                    token_path.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    creds: Credentials | None = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), list(scopes))

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            low = str(e).lower()
            # OAuth *client* removed/recreated in Cloud Console, or token revoked — not a local path issue.
            if "deleted_client" in low or "invalid_grant" in low:
                try:
                    token_path.unlink(missing_ok=True)
                except OSError:
                    pass
                creds = None
            # Token was minted with narrower scopes (e.g. metadata-only); cannot upgrade via refresh.
            elif "invalid_scope" in low:
                try:
                    token_path.unlink(missing_ok=True)
                except OSError:
                    pass
                creds = None
            else:
                raise
        else:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds

    if open_browser is None:
        ob = (os.environ.get("GOOGLE_OAUTH_OPEN_BROWSER") or "1").strip().lower()
        open_browser = ob not in ("0", "false", "no", "off")

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), list(scopes))
    creds = flow.run_local_server(
        port=_run_local_server_port(client_secrets),
        prompt="consent",
        open_browser=open_browser,
    )
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    if login_only:
        print(f"Saved OAuth token to {token_path}")
    return creds


def build_drive_service(creds: Credentials):
    """Return a Drive API v3 service object."""
    return build("drive", "v3", credentials=creds, cache_discovery=False)
