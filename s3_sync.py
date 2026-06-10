"""Upload a local directory tree to AWS S3 and optionally delete local files.

Credentials are read from (in order):
  1. Environment variables: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
  2. ~/.aws/credentials + ~/.aws/config (standard AWS CLI profile)

The S3 prefix is auto-generated as:  <org_domain>_<YYYY-MM-DD>
e.g. for admin@lh2holdings.com on 2026-05-16 → lh2holdings_2026-05-16/

Usage (as a module):
  from s3_sync import S3Syncer
  syncer = S3Syncer(bucket="my-bucket", admin_email="admin@co.com")
  syncer.upload_dir(local_dir, s3_sub_path="alice_at_co_com")
  syncer.delete_local(local_dir)
"""

from __future__ import annotations

import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _org_domain(admin_email: str) -> str:
    """Extract domain from admin email, e.g. 'lh2holdings.com' → 'lh2holdings'."""
    try:
        domain = admin_email.split("@", 1)[1]
        return domain.split(".")[0]
    except Exception:
        return "org"


def _auto_prefix(admin_email: str) -> str:
    """Generate S3 prefix: <domain>_<YYYY-MM-DD>"""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{_org_domain(admin_email)}_{date_str}"


# ---------------------------------------------------------------------------
# S3Syncer
# ---------------------------------------------------------------------------

class S3Syncer:
    """Upload local directories to S3 with parallel transfers and optional cleanup."""

    def __init__(
        self,
        *,
        bucket: str,
        admin_email: str,
        prefix: str = "",
        max_workers: int = 8,
        log: Any = None,
    ) -> None:
        """
        Parameters
        ----------
        bucket
            S3 bucket name (must already exist).
        admin_email
            Used to auto-generate the S3 prefix if ``prefix`` is empty.
        prefix
            Override the top-level S3 key prefix. If empty, auto-generated.
        max_workers
            Parallel upload threads (default: 8).
        log
            Optional callable for progress messages.
        """
        try:
            import boto3
        except ImportError:
            raise ImportError(
                "boto3 is required for S3 uploads. Install it with:\n"
                "  pip install boto3"
            )

        self._bucket    = bucket
        self._prefix    = (prefix or _auto_prefix(admin_email)).rstrip("/")
        self._workers   = max_workers
        self._log_fn    = log
        self._s3        = boto3.client("s3")
        self._lock      = threading.Lock()
        self._uploaded  = 0
        self._failed    = 0

    def _log(self, msg: str) -> None:
        if self._log_fn:
            self._log_fn(msg)

    @property
    def prefix(self) -> str:
        return self._prefix

    def _s3_key(self, local_path: Path, local_root: Path, s3_sub: str) -> str:
        rel = local_path.relative_to(local_root).as_posix()
        parts = [p for p in [self._prefix, s3_sub, rel] if p]
        return "/".join(parts)

    def _upload_file(self, local_path: Path, s3_key: str) -> bool:
        try:
            self._s3.upload_file(str(local_path), self._bucket, s3_key)
            with self._lock:
                self._uploaded += 1
            return True
        except Exception as e:
            self._log(f"[s3] ERROR uploading {local_path.name} → s3://{self._bucket}/{s3_key}: {e}")
            with self._lock:
                self._failed += 1
            return False

    def upload_dir(
        self,
        local_dir: Path,
        *,
        s3_sub_path: str = "",
    ) -> tuple[int, int]:
        """Upload all files in ``local_dir`` recursively to S3.

        Parameters
        ----------
        local_dir
            Local directory to upload.
        s3_sub_path
            Sub-path appended after the prefix, e.g. ``alice_at_co_com``.

        Returns
        -------
        (uploaded_count, failed_count)
        """
        local_dir = Path(local_dir)
        if not local_dir.is_dir():
            self._log(f"[s3] skip: {local_dir} does not exist")
            return 0, 0

        files = [f for f in local_dir.rglob("*") if f.is_file()]
        uploaded_before = self._uploaded
        failed_before = self._failed
        self._log(f"[s3] uploading {len(files)} files from {local_dir} → "
                  f"s3://{self._bucket}/{self._prefix}/{s3_sub_path}")

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = {
                pool.submit(
                    self._upload_file,
                    f,
                    self._s3_key(f, local_dir, s3_sub_path),
                ): f
                for f in files
            }
            done = 0
            for fut in as_completed(futures):
                done += 1
                if done % 100 == 0:
                    self._log(f"[s3] {done}/{len(files)} uploaded...")

        batch_uploaded = self._uploaded - uploaded_before
        batch_failed = self._failed - failed_before
        self._log(f"[s3] upload complete: {batch_uploaded} ok, {batch_failed} failed")
        return batch_uploaded, batch_failed

    def upload_file(self, local_file: Path, *, s3_sub_path: str = "") -> bool:
        """Upload a single file to S3."""
        s3_key = self._s3_key(local_file, local_file.parent, s3_sub_path)
        return self._upload_file(local_file, s3_key)

    @staticmethod
    def delete_local(local_dir: Path, *, log: Any = None) -> None:
        """Delete a local directory tree to free disk space after S3 upload."""
        try:
            shutil.rmtree(str(local_dir))
            if log:
                log(f"[s3] deleted local: {local_dir}")
        except Exception as e:
            if log:
                log(f"[s3] warn: could not delete {local_dir}: {e}")

    def sync_user(
        self,
        user_dir: Path,
        *,
        email: str,
        delete_after: bool = True,
    ) -> bool:
        """Upload a complete user output directory to S3 then optionally delete it.

        Returns True if upload succeeded (zero failures).
        """
        s3_sub = email.replace("@", "_at_").replace(".", "_")
        uploaded, failed = self.upload_dir(user_dir, s3_sub_path=s3_sub)
        success = failed == 0
        if success and uploaded == 0:
            self._log(f"[s3] warn: {email} — nothing to upload from {user_dir}")
        if success and delete_after and uploaded > 0:
            self.delete_local(user_dir, log=self._log_fn)
        elif failed > 0:
            self._log(f"[s3] warn: {email} had {failed} failed uploads — local files kept")
        return success and uploaded > 0
