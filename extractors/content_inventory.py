"""Count embedded structures per artifact (tables, charts, images)."""

from __future__ import annotations

import re
import zipfile
from collections import defaultdict
from pathlib import Path

from extractors.modality import AUDIO_VIDEO_EXTENSIONS, IMAGE_EXTENSIONS

_MAX_HTML_BYTES = 2_000_000
_MAX_TEXT_ROWS_BYTES = 5_000_000


def _merge(dst: dict[str, int], src: dict[str, int]) -> None:
    for k, v in src.items():
        if v:
            dst[k] = dst.get(k, 0) + int(v)


def _nonzero(d: dict[str, int]) -> dict[str, int]:
    return {k: v for k, v in d.items() if v > 0}


def format_content_inventory_cell(counts: dict[str, int]) -> str:
    """Human-readable cell: ``png: 4; tables: 2`` (sorted keys, skips zeros)."""
    nz = _nonzero(counts)
    if not nz:
        return ""
    parts = [f"{k}: {nz[k]}" for k in sorted(nz)]
    return "; ".join(parts)


def _standalone_media(path: Path) -> dict[str, int]:
    ext = path.suffix.lower().lstrip(".")
    out: dict[str, int] = {}
    if ext in IMAGE_EXTENSIONS:
        out["images"] = 1
        if ext in ("jpg", "jpeg"):
            out["jpeg"] = 1
        elif ext == "png":
            out["png"] = 1
        elif ext == "gif":
            out["gif"] = 1
        elif ext == "svg":
            out["svg"] = 1
        elif ext == "webp":
            out["webp"] = 1
        elif ext in ("bmp", "tif", "tiff"):
            out["bmp"] = 1
        else:
            out["other_image"] = 1
    elif ext in AUDIO_VIDEO_EXTENSIONS:
        out["video_audio"] = 1
    return out


def _count_html(path: Path, *, max_bytes: int = _MAX_HTML_BYTES) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    try:
        raw = path.read_bytes()[:max_bytes]
    except OSError:
        return {}
    text = raw.decode("utf-8", errors="ignore")
    low = text.lower()
    out["tables"] += len(re.findall(r"<table\b", low))
    out["canvas"] += len(re.findall(r"<canvas\b", low))
    img_tags = re.findall(r"<img\b[^>]*>", text, flags=re.IGNORECASE)
    out["images"] += len(img_tags)
    for tag in img_tags:
        sm = re.search(r'\bsrc\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if not sm:
            out["other_image"] += 1
            continue
        src = sm.group(1).split("?", 1)[0].lower()
        if src.endswith(".png"):
            out["png"] += 1
        elif src.endswith((".jpg", ".jpeg")):
            out["jpeg"] += 1
        elif src.endswith(".gif"):
            out["gif"] += 1
        elif src.endswith(".webp"):
            out["webp"] += 1
        elif src.endswith(".svg"):
            out["svg"] += 1
        elif src.endswith((".bmp", ".tif", ".tiff")):
            out["bmp"] += 1
        elif src.startswith("data:image/png"):
            out["png"] += 1
        elif src.startswith(("data:image/jpeg", "data:image/jpg")):
            out["jpeg"] += 1
        elif src.startswith("data:image/gif"):
            out["gif"] += 1
        elif src.startswith("data:image/svg"):
            out["svg"] += 1
        elif src.startswith("data:image/webp"):
            out["webp"] += 1
        else:
            out["other_image"] += 1
    out["svg"] += len(re.findall(r"<svg\b", low))
    return dict(out)


def _count_docx_zip(path: Path) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            for n in names:
                if not n.startswith("word/media/"):
                    continue
                suf = Path(n).suffix.lower().lstrip(".")
                out["images"] += 1
                if suf in ("jpg", "jpeg"):
                    out["jpeg"] += 1
                elif suf == "png":
                    out["png"] += 1
                elif suf == "gif":
                    out["gif"] += 1
                elif suf == "svg":
                    out["svg"] += 1
                elif suf == "webp":
                    out["webp"] += 1
                elif suf in ("bmp", "tif", "tiff"):
                    out["bmp"] += 1
                elif suf == "emf":
                    out["emf"] += 1
                elif suf == "wmf":
                    out["wmf"] += 1
                else:
                    out["other_image"] += 1
            for n in names:
                if n.startswith("word/charts/chart") and n.endswith(".xml"):
                    out["charts"] += 1
            doc_xml = next((n for n in names if n == "word/document.xml"), None)
            if doc_xml:
                body = zf.read(doc_xml)
                out["tables"] += body.count(b"<w:tbl")
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError):
        return {}
    return dict(out)


def _count_pptx_zip(path: Path) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            for n in names:
                if n.startswith("ppt/media/"):
                    suf = Path(n).suffix.lower().lstrip(".")
                    out["images"] += 1
                    if suf in ("jpg", "jpeg"):
                        out["jpeg"] += 1
                    elif suf == "png":
                        out["png"] += 1
                    elif suf == "gif":
                        out["gif"] += 1
                    elif suf == "svg":
                        out["svg"] += 1
                    elif suf == "webp":
                        out["webp"] += 1
                    elif suf in ("bmp", "tif", "tiff"):
                        out["bmp"] += 1
                    elif suf == "emf":
                        out["emf"] += 1
                    elif suf == "wmf":
                        out["wmf"] += 1
                    else:
                        out["other_image"] += 1
                if n.startswith("ppt/charts/chart") and n.endswith(".xml"):
                    out["charts"] += 1
                if n.startswith("ppt/slides/slide") and n.endswith(".xml"):
                    body = zf.read(n)
                    out["tables"] += body.count(b"<a:tbl")
            out["slides"] = len({n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)})
        if out["slides"] == 0:
            try:
                from pptx import Presentation  # type: ignore

                prs = Presentation(str(path))
                out["slides"] = len(prs.slides)
            except Exception:
                pass
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return {}
    return dict(out)


def _count_xlsx_zip(path: Path) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            info_map = {zi.filename: zi for zi in zf.infolist()}
            for n in names:
                if n.startswith("xl/media/"):
                    suf = Path(n).suffix.lower().lstrip(".")
                    out["images"] += 1
                    if suf in ("jpg", "jpeg"):
                        out["jpeg"] += 1
                    elif suf == "png":
                        out["png"] += 1
                    elif suf == "gif":
                        out["gif"] += 1
                    elif suf == "svg":
                        out["svg"] += 1
                    elif suf == "webp":
                        out["webp"] += 1
                    elif suf in ("bmp", "tif", "tiff"):
                        out["bmp"] += 1
                    elif suf == "emf":
                        out["emf"] += 1
                    elif suf == "wmf":
                        out["wmf"] += 1
                    else:
                        out["other_image"] += 1
                if n.startswith("xl/charts/chart") and n.endswith(".xml"):
                    out["charts"] += 1
            # Count worksheets and how many have actual data.
            # A worksheet XML with data is noticeably larger than the empty-sheet skeleton
            # (~900 bytes). 1 500 bytes is a safe threshold for "has at least one data row".
            _DATA_BYTES_THRESHOLD = 1_500
            ws_names = [n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
            out["worksheets"] = len(ws_names)
            data_sheets = sum(
                1 for n in ws_names
                if info_map.get(n) and info_map[n].file_size >= _DATA_BYTES_THRESHOLD
            )
            if data_sheets:
                out["data_sheets"] = data_sheets
        if out["worksheets"] == 0:
            try:
                import openpyxl

                wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
                try:
                    out["worksheets"] = len(wb.sheetnames)
                finally:
                    wb.close()
            except Exception:
                pass
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return {}
    return dict(out)


def _count_delimited_rows(path: Path, *, max_bytes: int = _MAX_TEXT_ROWS_BYTES) -> dict[str, int]:
    try:
        raw = path.read_bytes()[:max_bytes]
        text = raw.decode("utf-8", errors="ignore")
        if not text.strip():
            return {}
        return {"rows": len(text.splitlines())}
    except OSError:
        return {}


def _count_pdf(path: Path) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(path), strict=False)
        out["pdf_pages"] = len(reader.pages)
        image_count = 0
        for page in reader.pages:
            images = getattr(page, "images", None)
            if images:
                image_count += len(images)
        if image_count:
            out["images"] = image_count
    except Exception:
        return {}
    return dict(out)


def gather_content_inventory(path: Path, *, max_html_bytes: int = _MAX_HTML_BYTES) -> dict[str, int]:
    """Return non-negative counts keyed by metric name."""
    ext = path.suffix.lower().lstrip(".")
    merged: dict[str, int] = {}

    if ext in IMAGE_EXTENSIONS or ext in AUDIO_VIDEO_EXTENSIONS:
        _merge(merged, _standalone_media(path))
        return _nonzero(merged)

    if ext in ("html", "htm"):
        _merge(merged, _count_html(path, max_bytes=max_html_bytes))
        return _nonzero(merged)

    if ext == "docx":
        _merge(merged, _count_docx_zip(path))
        return _nonzero(merged)

    if ext == "pptx":
        _merge(merged, _count_pptx_zip(path))
        return _nonzero(merged)

    if ext in ("xlsx", "xlsm"):
        _merge(merged, _count_xlsx_zip(path))
        return _nonzero(merged)

    if ext == "pdf":
        _merge(merged, _count_pdf(path))
        return _nonzero(merged)

    if ext in ("csv", "tsv"):
        _merge(merged, _count_delimited_rows(path))
        return _nonzero(merged)

    return {}
