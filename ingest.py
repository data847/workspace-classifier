from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Artifact:
    artifact_id: int
    path: str
    filename: str
    extension: str
    size_bytes: int
    modified_time_unix: float
    source_guess: str | None

    def to_dict(self) -> dict:
        return asdict(self)


_SOURCE_HINTS: list[tuple[str, str]] = [
    # Common exports / systems
    ("slack", "Slack"),
    ("teams", "Microsoft Teams"),
    ("microsoft teams", "Microsoft Teams"),
    ("notion", "Notion"),
    ("confluence", "Confluence"),
    ("jira", "Jira"),
    ("linear", "Linear"),
    ("github", "GitHub"),
    ("gitlab", "GitLab"),
    ("gdrive", "Google Drive"),
    ("google drive", "Google Drive"),
    ("drive", "Google Drive"),
    ("gmail", "Gmail"),
    ("outlook", "Outlook"),
    ("zendesk", "Zendesk"),
    ("freshdesk", "Freshdesk"),
    ("hubspot", "HubSpot"),
    ("salesforce", "Salesforce"),
    ("pipedrive", "Pipedrive"),
    ("gong", "Gong"),
    ("chorus", "Chorus"),
    ("zoom", "Zoom"),
    ("google meet", "Google Meet"),
    ("meet", "Google Meet"),
    ("otter", "Otter"),
    ("fireflies", "Fireflies"),
    ("fathom", "Fathom"),
    ("loom", "Loom"),
    ("docusign", "DocuSign"),
    ("pandadoc", "PandaDoc"),
    ("carta", "Carta"),
    ("digify", "Digify"),
    ("quickbooks", "QuickBooks"),
    ("xero", "Xero"),
    ("tally", "Tally"),
    ("workday", "Workday"),
    ("greenhouse", "Greenhouse"),
    ("bamboohr", "BambooHR"),
    ("darwinbox", "Darwinbox"),
    ("keka", "Keka"),
    ("aws", "AWS"),
    ("gcp", "GCP"),
]


def infer_source_system(path: str) -> str | None:
    """Best-effort source inference from the file path."""
    lowered = path.lower()
    for needle, system in _SOURCE_HINTS:
        if needle in lowered:
            return system
    return None


def iter_files(
    root: str,
    *,
    exclude_dirs: Iterable[str] = (".venv", "__pycache__"),
    include_hidden: bool = False,
) -> Iterable[Path]:
    """Yield files under root.

    Defaults are conservative (skip dot-dirs/files + node_modules) to avoid
    accidental massive scans during development. For strict runs, set
    include_hidden=True and exclude_dirs=().
    """
    root_path = Path(root).expanduser().resolve()
    for dirpath, dirnames, filenames in os.walk(root_path):
        # prune excluded directories
        if include_hidden:
            dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        else:
            dirnames[:] = [
                d
                for d in dirnames
                if d not in exclude_dirs and not d.startswith(".") and d != "node_modules"
            ]
        # Ensure deterministic traversal order for stable artifact_id assignment.
        dirnames.sort()
        filenames.sort()
        for fn in filenames:
            if (not include_hidden) and fn.startswith("."):
                continue
            p = Path(dirpath) / fn
            if p.is_file():
                yield p


def scan_dump_folder(
    dump_path: str,
    *,
    max_files: int | None = None,
    exclude_dirs: Iterable[str] = (".venv", "__pycache__"),
    include_hidden: bool = False,
) -> list[Artifact]:
    """Scan a dump folder and return Artifact rows (stable ordering)."""
    artifacts: list[Artifact] = []
    i = 0
    for p in iter_files(
        dump_path, exclude_dirs=exclude_dirs, include_hidden=include_hidden
    ):
        stat = p.stat()
        i += 1
        artifacts.append(
            Artifact(
                artifact_id=i,
                path=str(p),
                filename=p.name,
                extension=p.suffix.lower().lstrip("."),
                size_bytes=int(stat.st_size),
                modified_time_unix=float(stat.st_mtime),
                source_guess=infer_source_system(str(p)),
            )
        )
        if max_files is not None and len(artifacts) >= max_files:
            break

    return artifacts

