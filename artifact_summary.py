from __future__ import annotations

import re
import unicodedata

_PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----")
_KUBECONFIG_RE = re.compile(r"apiVersion:\s*v1\s*\n.*?clusters:", re.IGNORECASE | re.DOTALL)
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"  # flags
    "\U0001F300-\U0001FAFF"  # emoji & symbols (broad)
    "\U00002700-\U000027BF"  # dingbats
    "\U00002600-\U000026FF"  # misc symbols
    "]+",
    flags=re.UNICODE,
)

_BOILERPLATE_LINE_RE = re.compile(
    r"^\s*(?:"
    r"dear\s+(?:sir|madam|sir/madam|madame|monsieur|madam/sir)"
    r"|to\s+whom\s+it\s+may\s+concern"
    r"|hello|hi|hey"
    r"|bonjour|bonsoir|salut"
    r"|madam|sir"
    r")\b[,:]?\s*$",
    flags=re.IGNORECASE,
)

_LETTER_HEADER_RE = re.compile(
    r"^\s*(?:"
    r"dear\s+"
    r"|subject\s*:?"
    r"|re\s*:?"
    r"|date\s*:?"
    r"|from\s*:?"
    r"|to\s*:?"
    r"|cc\s*:?"
    r")",
    flags=re.IGNORECASE,
)

def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    def fold(s: str) -> str:
        s = (s or "").replace("\ufe0f", "")
        s = _EMOJI_RE.sub(" ", s)
        s = unicodedata.normalize("NFKD", s)
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
        return " ".join(s.lower().split())

    t = fold(text)
    ns = [fold(n) for n in needles]
    return any(n and (n in t) for n in ns)


def _topic_from_path(filename: str, rel_path: str) -> str | None:
    """Infer a short phrase from filename/path cues (deterministic)."""
    lower = f"{rel_path}/{filename}"

    # Legal / corp DD
    if _has_any(lower, ("dd", "due diligence", "diligence", "m&a", "ma ", "acquisition", "merger")) and _has_any(
        lower, ("checklist", "request", "requests", "list", "question", "q&a", "qa")
    ):
        return "Legal DD request checklist for M&A review."
    if _has_any(lower, ("cap table", "captable", "cap_table", "table de capitalisation", "capitalisation")):
        return "Cap table showing ownership structure."
    if _has_any(lower, ("kbis", "extrait kbis")):
        return "French company registration extract (KBIS)."
    if _has_any(lower, ("rcs", "registre du commerce", "commercial registry", "registry extract")):
        return "Commercial registry extract."
    if _has_any(lower, ("no bankruptcy", "non bankruptcy", "non-faillite", "non faillite", "no_bankruptcy")):
        return "No-bankruptcy certificate."
    if _has_any(lower, ("shareholder", "shareholders")) and _has_any(lower, ("unanimous", "decision", "resolution")):
        return "Shareholders unanimous decision agreement."
    if _has_any(lower, ("statuts", "bylaws", "articles of association")):
        return "Corporate regulatory document."
    if _has_any(lower, ("indebtedness", "endettement", "debt statement", "dettes")):
        return "Statement of company indebtedness."

    # HR
    if _has_any(lower, ("payslip", "bulletin", "bulletin de paie", "fiche de paie", "pay slip")):
        return "Individual employee payslip."
    if _has_any(lower, ("mutuelle", "health insurance", "complémentaire santé", "complementaire sante", "assurance sante", "prevoyance")):
        return "Health insurance (mutuelle) document."
    if _has_any(lower, ("resignation", "démission", "demission", "lettre de demission")):
        return "Employee resignation letter."
    if _has_any(lower, ("termination", "departure", "rupture", "solde de tout compte", "pole emploi", "pôle emploi")):
        return "Employment/departure document."
    if _has_any(lower, ("contract", "contrat")) and _has_any(lower, ("employment", "travail", "cdi", "cdd", "hr", "rh")):
        return "Employment contract for employee."

    # Finance / accounting
    if _has_any(lower, ("loan", "amortization", "amortisation", "repayment", "échéancier", "echeancier", "calendrier de remboursement")):
        return "Loan repayment schedule."
    if _has_any(lower, ("depreciation", "amortissement", "immobilisation", "fixed asset")):
        return "Fixed asset depreciation schedule."
    if _has_any(lower, ("cir", "credit d'impot recherche", "crédit d'impôt recherche")):
        return "R&D tax credit (CIR) calculation worksheet."
    if _has_any(lower, ("balance sheet", "bilan", "actif", "passif")):
        return "Assets and liabilities statement."
    if _has_any(lower, ("insurance", "assurance", "rc pro", "responsabilite")) and _has_any(lower, ("certificate", "attestation")):
        return "Professional insurance certificate."

    # Photos / screenshots
    if _has_any(lower, ("screenshot", "capture", "photo", "image", "img_", "scan")):
        return "Screenshot / photo."

    return None


def _topic_from_snippet(snip: str, lower_path: str) -> str | None:
    """Return a short phrase based on concrete keywords in extracted text.

    This intentionally avoids copying arbitrary first lines from documents, which
    often contain salutations, headers, or table column names.
    """
    t = " ".join((snip or "").split())
    lp = lower_path

    if not t:
        return None

    # Tech / product / engineering
    if _has_any(t, ("cve-", "vulnerability", "threat", "security issue", "security", "compliance", "iso 27001", "soc 2")):
        return "Code security issue tracking and compliance checklists."
    if _has_any(t, ("kubernetes", "k8s", "cluster", "helm", "kubeconfig", "namespace", "deployment")):
        if _has_any(t, ("cost", "billing", "finops", "spend", "savings", "optimization", "chargeback")):
            return "Cluster cost analysis and cloud spending optimization strategies."
        return "Kubernetes cluster documentation and operational notes."
    if _has_any(t, ("aws", "gcp", "azure", "terraform", "cloudformation")) and _has_any(t, ("cost", "billing", "spend", "savings", "optimization")):
        return "Cloud cost analysis and spending optimization strategies."

    # Legal / corp / due diligence
    if _has_any(t, ("due diligence", "dd request", "dd checklist", "legal dd", "diligence", "audit", "liste de demandes", "liste des demandes")):
        return "Legal due diligence request checklist for M&A review."
    if _has_any(t, ("cap table", "capitalization table", "table de capitalisation", "cap-table")):
        return "Cap table showing ownership structure."
    if _has_any(t, ("no bankruptcy", "non-bankruptcy", "non faillite", "absence de faillite", "attestation de non faillite", "certificat de non faillite")):
        return "No-bankruptcy certificate."
    if _has_any(
        t,
        (
            "commercial register",
            "registry extract",
            "registre du commerce",
            "registre du commerce et des societes",
            "rcs",
            "r.c.s",
            "kbis",
            "extrait kbis",
        ),
    ):
        # Prefer a KBIS phrasing when the term appears.
        if "kbis" in (t or "").lower() or "extrait kbis" in (t or "").lower():
            return "French company registration extract (KBIS)."
        return "Commercial registry extract."
    if _has_any(t, ("indebtedness", "statement of indebtedness", "endettement", "dettes")):
        return "Statement of company indebtedness."
    if _has_any(t, ("unanimous decision", "décision unanime", "decision unanime", "resolution unanime", "proces verbal")):
        return "Shareholders unanimous decision agreement."
    if _has_any(t, ("shareholder register", "registre des actionnaires", "registre actionnaires")):
        return "Shareholders register."
    if _has_any(t, ("shareholder accounts", "compte courant d'associ", "compte courant d'associ")):
        return "Shareholder accounts register."
    if _has_any(t, ("articles of association", "statuts", "bylaws")):
        return "Corporate regulatory document."

    # HR
    if _has_any(t, ("resignation", "démission", "demission", "lettre de demission")):
        return "Employee resignation letter."
    if _has_any(t, ("mutuelle", "complémentaire santé", "complementaire sante", "assurance sante", "prevoyance", "assurance maladie")):
        return "Health insurance (mutuelle) document."
    if _has_any(t, ("payslip", "pay slip", "bulletin de paie", "fiche de paie")):
        return "Individual employee payslip."
    if _has_any(t, ("employment contract", "contrat de travail", "cdi", "cdd")):
        if "cdi" in t or " cdi " in f" {t} ":
            return "Employment contract (CDI) for employee."
        return "Employment contract for employee."
    if _has_any(t, ("termination", "departure", "rupture", "solde de tout compte", "attestation pôle emploi", "attestation pole emploi", "pole emploi", "certificat de travail", "attestation employeur")):
        return "Employment/departure document."

    # Finance
    if _has_any(t, ("loan", "repayment schedule", "amortization", "amortisation")):
        return "Loan repayment schedule."
    if _has_any(t, ("fixed asset", "depreciation", "immobilisation", "amortissement")):
        return "Fixed asset depreciation schedule."
    if _has_any(t, ("professional insurance", "assurance", "responsibility", "rc pro", "responsabilite")):
        return "Professional insurance certificate."
    if _has_any(t, ("r&d tax", "cir", "crédit d'impôt recherche", "credit d'impot recherche")):
        return "R&D tax credit (CIR) calculation worksheet."
    if _has_any(t, ("assets and liabilities", "actif et passif", "bilan")):
        return "Assets and liabilities statement."

    # Misc media / ops
    if _has_any(lp, ("photo", "image", "screenshot")) or _has_any(t, ("screenshot", "capture d'écran", "capture d'ecran", "capture d'ecran")):
        return "Screenshot / photo."

    return None

_MONTH_YEAR_RE = re.compile(
    r"(?:(?:0?[1-9]|1[0-2])[-_/](?:20)?\d{2})|(?:(?:20)?\d{2}[-_/](?:0?[1-9]|1[0-2]))"
)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _compact(s: str, limit: int = 120) -> str:
    s = " ".join((s or "").replace("\r", " ").replace("\n", " ").split())
    s = _EMOJI_RE.sub(" ", s)
    s = s.replace("\u00ad", "").replace("\u200b", "").replace("\ufe0f", "")
    s = " ".join(s.split())
    if len(s) <= limit:
        return s

    cut = max(0, limit - 3)
    boundary = s.rfind(" ", 0, cut)
    if boundary >= max(20, int(cut * 0.6)):
        return s[:boundary].rstrip() + "..."
    return s[:cut].rstrip() + "..."


def _looks_like_title(line: str) -> bool:
    t = line.strip()
    if len(t) < 12:
        return True
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return True
    upper = sum(1 for c in letters if c.isupper())
    # Mostly uppercase -> likely a title/header (even if long).
    if upper / max(1, len(letters)) > 0.85 and len(t.split()) <= 14:
        return True
    return False


def _summary_from_snippet(snip: str) -> str | None:
    """Pick a single, meaningful line from extracted text (no guessing)."""
    if not snip or not snip.strip():
        return None

    lines = [ln.strip() for ln in snip.replace("\r", "\n").split("\n")]
    # Drop empties
    lines = [ln for ln in lines if ln]
    if not lines:
        return None

    # Prefer the first non-title line with some substance.
    for ln in lines[:40]:
        if _looks_like_title(ln):
            continue
        # Skip common boilerplate headers / salutations.
        if _BOILERPLATE_LINE_RE.match(ln):
            continue
        if _LETTER_HEADER_RE.match(ln) and len(ln) < 80:
            continue
        # Avoid pure URLs or code-ish noise
        if ln.lower().startswith(("http://", "https://")):
            continue
        if len(ln) >= 18:
            return _compact(ln, limit=160)

    for ln in lines[:25]:
        if _looks_like_title(ln):
            continue
        if ln.lower().startswith(("http://", "https://")):
            continue
        if _BOILERPLATE_LINE_RE.match(ln):
            continue
        if _LETTER_HEADER_RE.match(ln) and len(ln) < 80:
            continue
        if len(ln) < 18:
            continue
        return _compact(ln, limit=160)
    return None


def _guess_month_year(text: str) -> str | None:
    m = _MONTH_YEAR_RE.search(text)
    if m:
        return m.group(0).replace("_", "-").replace("/", "-")
    y = _YEAR_RE.search(text)
    return y.group(1) if y else None


def summarize_artifact(*, filename: str, rel_path: str, extension: str, snippet: str | None) -> str:
    """Return a short, clean summary string (no newlines)."""
    lower = f"{rel_path}/{filename}".lower()
    ext = (extension or "").lower().strip().lstrip(".")
    if not ext and "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower().strip().lstrip(".")
    snip = snippet or ""

    # Explicit security warnings only when we have concrete markers.
    if _PRIVATE_KEY_RE.search(snip) or "private key" in lower:
        return "WARNING: Private key material detected. Exclude from licensing."
    if _KUBECONFIG_RE.search(snip) or "kubeconfig" in lower:
        return "WARNING: Kubernetes cluster credentials detected. Exclude from licensing."

    # First: filename/path cues (best effort, deterministic).
    path_topic = _topic_from_path(filename, rel_path)
    if path_topic:
        return path_topic

    # Prefer deterministic topic phrases inferred from keywords, not arbitrary lines.
    topic = _topic_from_snippet(snip, lower)
    if topic:
        return topic

    # HR / legal / finance doc types
    if "bulletin" in lower or "payslip" in lower:
        d = _guess_month_year(filename) or _guess_month_year(rel_path)
        return _compact(f"Payslip{f' ({d})' if d else ''}.")
    if "dsn" in lower:
        d = _guess_month_year(filename) or _guess_month_year(rel_path)
        return _compact(f"Monthly social declaration (DSN){f' ({d})' if d else ''}.")
    if "urssaf" in lower:
        d = _guess_month_year(filename) or _guess_month_year(rel_path)
        return _compact(f"URSSAF contribution declaration{f' ({d})' if d else ''}.")
    if "rib" in lower or "iban" in lower:
        return "Bank details document."
    if "cni" in lower or "passport" in lower or "carte vitale" in lower:
        return "Personal identity document."
    if "cv" in lower or "resume" in lower:
        return "Team member CV / resume."
    # Employment contracts only when we have HR/employment signals.
    if (
        "cdi" in lower
        or "cdd" in lower
        or "contrat de travail" in lower
        or "employment contract" in lower
        or ("contrat" in lower and ("hr" in lower or "rh" in lower))
        or ("contract" in lower and ("hr" in lower or "employment" in lower))
        or ("contrat de travail" in (snip or "").lower())
        or ("employment contract" in (snip or "").lower())
    ):
        if "avenant" in lower or "amend" in lower:
            return "Employment contract amendment."
        if "stage" in lower or "intern" in lower:
            return "Internship agreement."
        return "Employment contract."
    if "mutuelle" in lower or "prevoyance" in lower or "insurance" in lower:
        return "Health / insurance documentation."
    if ext in {"xlsx", "xls"} and ("bspce" in lower or "bsa" in lower or "cap" in lower):
        return "Equity / options tracking spreadsheet."

    # If we extracted text but couldn't map it, avoid "random" summaries — return a safe generic label.
    if snip and snip.strip() and ext in {"pdf", "doc", "docx", "ppt", "pptx"}:
        if "legal" in lower or "contract" in lower or "contrat" in lower:
            return "Company legal document."
        if "hr" in lower or "rh" in lower:
            return "HR document."
        return "Company document."

    # Fallback: file type summary without guessing content.
    if ext == "eml":
        st = (snip or "").strip()
        if st:
            return _compact(f"Email (.eml). Subject: {st}", limit=200)
        return "Email message (.eml)."
    if ext in {"png", "jpg", "jpeg", "webp", "gif", "tiff", "bmp", "heic"}:
        return "Photo / image."
    if ext in {"mp4", "mov", "m4v", "avi", "mkv", "webm"}:
        return "Video file."
    if ext in {"mp3", "wav", "m4a", "aac", "flac", "ogg"}:
        return "Audio recording."
    if ext in {"zip", "7z", "rar", "tar", "gz", "tgz", "bz2", "xz"}:
        return "Archive file."
    if ext in {"pdf", "doc", "docx", "ppt", "pptx"}:
        return "Company document."
    if ext in {"xlsx", "xlsm", "xls", "csv", "tsv"}:
        return "Company spreadsheet."
    if ext in {"key", "pem", "crt", "cer", "p12", "pfx"}:
        return "Certificate / key material."
    if ext in {"sql"}:
        return "Database script / SQL export."
    if ext in {"py", "js", "ts", "tsx", "java", "go", "rb", "php", "c", "cc", "cpp", "h", "hpp", "rs"}:
        return "Source code file."
    if ext in {"yml", "yaml", "toml", "ini", "conf", "config", "env"}:
        return "Configuration file."
    if ext in {"json"}:
        return "JSON data file."
    if ext in {"log"}:
        return "Log file."
    if ext:
        return _compact(f"{ext.upper()} file.")
    return "Company file."

