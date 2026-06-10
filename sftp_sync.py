"""Upload a local directory tree to Hetzner Storage Box via SFTP.

Credentials are read from environment variables (typically ``operator.env``,
same format as drivetocloud / cloud_transfer):

  SFTP_HOST, SFTP_PORT, SFTP_USERNAME, SFTP_PASSWORD, SFTP_BASE_PATH

The remote prefix is auto-generated as:  <org_domain>_<YYYY-MM-DD>
under ``SFTP_BASE_PATH`` (default: ``workspace``).

Usage::

  from sftp_sync import SftpSyncer
  syncer = SftpSyncer.from_env(admin_email="admin@co.com")
  syncer.sync_user(user_dir, email="alice@co.com")
"""

from __future__ import annotations

import os
import shutil
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from s3_sync import _auto_prefix


class SftpSyncer:
    """Upload local directories to Hetzner Storage Box with parallel SFTP transfers."""

    def __init__(
        self,
        *,
        host: str,
        username: str,
        password: str,
        port: int = 23,
        base_path: str = "workspace",
        admin_email: str,
        prefix: str = "",
        max_workers: int = 8,
        log: Any = None,
    ) -> None:
        try:
            import paramiko  # noqa: F401
        except ImportError:
            raise ImportError(
                "paramiko is required for Hetzner SFTP uploads. Install it with:\n"
                "  pip install paramiko"
            )

        self._host = host.strip()
        self._port = int(port)
        self._username = username.strip()
        self._password = password
        self._base_path = base_path.strip("/")
        self._prefix = (prefix or _auto_prefix(admin_email)).strip("/")
        self._workers = max(1, int(max_workers))
        self._log_fn = log
        self._local = threading.local()
        self._known_dirs: set[str] = set()
        self._dir_lock = threading.Lock()
        self._connect_lock = threading.Lock()
        self._lock = threading.Lock()
        self._uploaded = 0
        self._failed = 0

    @classmethod
    def from_env(
        cls,
        *,
        admin_email: str,
        prefix: str = "",
        max_workers: int | None = None,
        log: Any = None,
    ) -> "SftpSyncer":
        host = (os.environ.get("SFTP_HOST") or "").strip()
        username = (os.environ.get("SFTP_USERNAME") or "").strip()
        password = (os.environ.get("SFTP_PASSWORD") or "").strip()
        if not host or not username or not password:
            raise ValueError(
                "Hetzner SFTP credentials missing. Set SFTP_HOST, SFTP_USERNAME, and "
                "SFTP_PASSWORD in operator.env (same format as drivetocloud)."
            )
        port = int((os.environ.get("SFTP_PORT") or "23").strip() or "23")
        base_path = (os.environ.get("SFTP_BASE_PATH") or "workspace").strip() or "workspace"
        workers = max_workers
        if workers is None:
            workers = int((os.environ.get("SFTP_WORKERS") or os.environ.get("PARALLEL_WORKERS") or "8").strip() or "8")
        return cls(
            host=host,
            port=port,
            username=username,
            password=password,
            base_path=base_path,
            admin_email=admin_email,
            prefix=prefix,
            max_workers=workers,
            log=log,
        )

    def _log(self, msg: str) -> None:
        if self._log_fn:
            self._log_fn(msg)

    @property
    def prefix(self) -> str:
        parts = [p for p in [self._base_path, self._prefix] if p]
        return "/".join(parts)

    def _sftp(self):
        import paramiko

        local = self._local
        transport_ok = (
            hasattr(local, "transport")
            and local.transport is not None
            and local.transport.is_active()
        )
        if not transport_ok:
            with self._connect_lock:
                sock = socket.create_connection((self._host, self._port), timeout=60)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                if hasattr(socket, "TCP_KEEPIDLE"):
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
                transport = paramiko.Transport(sock)
                transport.set_keepalive(30)
                transport.connect(username=self._username, password=self._password)
                local.transport = transport
                local.sftp = paramiko.SFTPClient.from_transport(transport)
        return local.sftp

    def close(self) -> None:
        if hasattr(self._local, "transport") and self._local.transport:
            self._local.transport.close()

    def _remote_key(self, local_path: Path, local_root: Path, remote_sub: str) -> str:
        rel = local_path.relative_to(local_root).as_posix()
        parts = [p for p in [self._base_path, self._prefix, remote_sub, rel] if p]
        return "/".join(parts)

    def _makedirs(self, remote_dir: str) -> None:
        if not remote_dir:
            return
        sftp = self._sftp()
        parts = [p for p in remote_dir.split("/") if p]
        path = ""
        for part in parts:
            path = f"{path}/{part}" if path else part
            with self._dir_lock:
                if path in self._known_dirs:
                    continue
            try:
                sftp.stat(path)
            except OSError:
                try:
                    sftp.mkdir(path)
                except OSError as mkdir_err:
                    err_lower = str(mkdir_err).lower()
                    if any(k in err_lower for k in ("quota", "no space", "permission denied", "read-only")):
                        raise RuntimeError(
                            f"Cannot create remote directory '{path}': {mkdir_err}"
                        ) from mkdir_err
                    for _ in range(3):
                        time.sleep(0.2)
                        try:
                            sftp.stat(path)
                            break
                        except OSError:
                            pass
                    else:
                        raise RuntimeError(
                            f"Cannot create remote directory '{path}': {mkdir_err}"
                        ) from mkdir_err
            with self._dir_lock:
                self._known_dirs.add(path)

    def _upload_file(self, local_path: Path, remote_key: str) -> bool:
        remote_key = remote_key.strip("/")
        try:
            self._makedirs(os.path.dirname(remote_key))
            self._sftp().put(str(local_path), remote_key, confirm=True)
            with self._lock:
                self._uploaded += 1
            return True
        except Exception as e:
            self._log(f"[hetzner] ERROR uploading {local_path.name} → sftp://{self._host}/{remote_key}: {e}")
            with self._lock:
                self._failed += 1
            return False

    def upload_dir(
        self,
        local_dir: Path,
        *,
        s3_sub_path: str = "",
    ) -> tuple[int, int]:
        local_dir = Path(local_dir)
        if not local_dir.is_dir():
            self._log(f"[hetzner] skip: {local_dir} does not exist")
            return 0, 0

        files = [f for f in local_dir.rglob("*") if f.is_file()]
        uploaded_before = self._uploaded
        failed_before = self._failed
        self._log(
            f"[hetzner] uploading {len(files)} files from {local_dir} → "
            f"sftp://{self._host}/{self.prefix}/{s3_sub_path}"
        )

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = {
                pool.submit(
                    self._upload_file,
                    f,
                    self._remote_key(f, local_dir, s3_sub_path),
                ): f
                for f in files
            }
            done = 0
            for fut in as_completed(futures):
                done += 1
                if done % 100 == 0:
                    self._log(f"[hetzner] {done}/{len(files)} uploaded...")

        batch_uploaded = self._uploaded - uploaded_before
        batch_failed = self._failed - failed_before
        self._log(f"[hetzner] upload complete: {batch_uploaded} ok, {batch_failed} failed")
        return batch_uploaded, batch_failed

    def upload_file(self, local_file: Path, *, s3_sub_path: str = "") -> bool:
        remote_key = self._remote_key(local_file, local_file.parent, s3_sub_path)
        return self._upload_file(local_file, remote_key)

    @staticmethod
    def delete_local(local_dir: Path, *, log: Any = None) -> None:
        try:
            shutil.rmtree(str(local_dir))
            if log:
                log(f"[hetzner] deleted local: {local_dir}")
        except Exception as e:
            if log:
                log(f"[hetzner] warn: could not delete {local_dir}: {e}")

    def sync_user(
        self,
        user_dir: Path,
        *,
        email: str,
        delete_after: bool = True,
    ) -> bool:
        remote_sub = email.replace("@", "_at_").replace(".", "_")
        uploaded, failed = self.upload_dir(user_dir, s3_sub_path=remote_sub)
        success = failed == 0
        if success and uploaded == 0:
            self._log(f"[hetzner] warn: {email} — nothing to upload from {user_dir}")
        if success and delete_after and uploaded > 0:
            self.delete_local(user_dir, log=self._log_fn)
        elif failed > 0:
            self._log(f"[hetzner] warn: {email} had {failed} failed uploads — local files kept")
        return success and uploaded > 0
