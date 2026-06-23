"""
================================================================================
  web_app.py — Main Web Application & HTTP API Server
================================================================================

  PURPOSE:
    Serves the credit application extraction UI and handles all HTTP API calls.
    This is the entry point — run this file to start the server.

  HOW TO RUN:
    python web_app.py
    Then open: http://127.0.0.1:8765 in your browser

  PAGES:
    GET /          — Main extraction UI (upload PDF/image, view extracted data)
    GET /scrub-only — Scrub-only tool (loads only_for_scrub.html.html)

  API ENDPOINTS:
    POST /api/extract      — Upload PDF or image, run OCR, return raw text
                             (PDFs go directly to Claude AI, images use OCR pipeline)
    POST /api/filter       — Send raw OCR text through AI, return structured JSON
    POST /api/export       — Save extracted values to Google Sheets
    POST /api/sheet-clear  — Clear all sheet data (password protected: "2580")

  DATA FLOW:
    PDF upload → Claude AI (direct) → JSON → Frontend form fill + Excel save
    Image upload → OCR pipeline → Raw text display → AI filter → JSON → form fill

  ARCHITECTURE:
    Uses Python's built-in http.server (no Flask/Django dependency).
    ThreadingHTTPServer allows multiple concurrent requests.

  ENVIRONMENT:
    PORT is configurable via OCR_WEB_PORT in .env (default: 8765)
    Host is always 127.0.0.1 (localhost only — not exposed to network)
================================================================================
"""

from __future__ import annotations

import json
import importlib
import mimetypes
import os
import re
import tempfile
import threading
import urllib.parse
import webbrowser
from datetime import date
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ai.extractor import ai_json_to_frontend_values, extract_ai_json, extract_ai_json_from_pdf
from google_sheet_sync import clear_credit_sheet_data, load_credit_env, try_append_credit_google_row


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Server Configuration
# ══════════════════════════════════════════════════════════════════════════════

HOST = "127.0.0.1"
load_credit_env()
PORT = int(os.getenv("OCR_WEB_PORT", "8765"))

APP_DIR              = Path(__file__).resolve().parent
EXCEL_EXPORT_PATH    = APP_DIR / "credit_export.xlsx"
RAW_TEXT_BLOCKLIST_PATH = Path(os.getenv("RAW_TEXT_BLOCKLIST_PATH", "raw_text_remove_lines.txt"))
RAW_TEXT_MAX_LINE_LENGTH = int(os.getenv("RAW_TEXT_MAX_LINE_LENGTH", "1000"))
SCRUB_ONLY_HTML_PATH = APP_DIR / "only_for_scrub.html.html"

_EXCEL_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Field Definitions
# ══════════════════════════════════════════════════════════════════════════════

# Business information fields (HTML input id → human label)
BUSINESS_FIELDS = [
    ("businessName",     "Business Legal Name"),
    ("businessDbaName",  "Business DBA Name"),
    ("businessState",    "State"),
    ("businessAddress",  "Business Address"),
    ("businessCity",     "City"),
    ("businessStateFull","State Full Name"),
    ("businessZip",      "Zip"),
    ("businessPhone",    "Business Phone Number"),
    ("einNumber",        "Tax ID (EIN #)"),
    ("businessEmail",    "Business Gmail"),
    ("lander_name",           "Lander Name"),
    ("representative_name",   "Representative Name"),
    ("iso_email",             "ISO Email"),
]

# Owner fields — same pattern for 4 owners with different prefixes
OWNER_FIELDS = [
    ("ownerName",    "Owner Name"),
    ("ownerDob",     "Owner DOB"),
    ("ownerAddress", "Home Address"),
    ("ownerCity",    "City"),
    ("ownerState",   "State"),
    ("ownerStateFull","State Full Name"),
    ("ownerZip",     "Zip"),
    ("ssn",          "SSN#"),
]
OWNER2_FIELDS = [
    ("owner2Name",    "2nd Owner Name"),
    ("owner2Dob",     "2nd Owner DOB"),
    ("owner2Address", "2nd Owner Home Address"),
    ("owner2City",    "2nd Owner City"),
    ("owner2State",   "2nd Owner State"),
    ("owner2StateFull","2nd Owner State Full Name"),
    ("owner2Zip",     "2nd Owner Zip"),
    ("owner2Ssn",     "2nd Owner SSN#"),
]
OWNER3_FIELDS = [
    ("owner3Name",    "3rd Owner Name"),
    ("owner3Dob",     "3rd Owner DOB"),
    ("owner3Address", "3rd Owner Home Address"),
    ("owner3City",    "3rd Owner City"),
    ("owner3State",   "3rd Owner State"),
    ("owner3StateFull","3rd Owner State Full Name"),
    ("owner3Zip",     "3rd Owner Zip"),
    ("owner3Ssn",     "3rd Owner SSN#"),
]
OWNER4_FIELDS = [
    ("owner4Name",    "4th Owner Name"),
    ("owner4Dob",     "4th Owner DOB"),
    ("owner4Address", "4th Owner Home Address"),
    ("owner4City",    "4th Owner City"),
    ("owner4State",   "4th Owner State"),
    ("owner4StateFull","4th Owner State Full Name"),
    ("owner4Zip",     "4th Owner Zip"),
    ("owner4Ssn",     "4th Owner SSN#"),
]

# Grouped by section for rendering the HTML form
FIELD_GROUPS = [
    ("Business Information",  BUSINESS_FIELDS),
    ("Owner 1 Information",   OWNER_FIELDS),
    ("2nd Owner Information", OWNER2_FIELDS),
    ("3rd Owner Information", OWNER3_FIELDS),
    ("4th Owner Information", OWNER4_FIELDS),
]

ALL_FIELDS = [item for _, fields in FIELD_GROUPS for item in fields]

# Fixed contact values — always hardcoded, user cannot override these
FIXED_BUSINESS_CONTACT = {
    "businessPhone": "6468459754",
    "businessEmail": "contracts@tvtcapital.com",
}

# Excel export column order
EXCEL_COLUMNS = [
    ("businessName",     "Business Legal Name"),
    ("businessDbaName",  "Business DBA Name"),
    ("businessAddress",  "Business Address"),
    ("businessState",    "Business State"),
    ("businessCity",     "Business City"),
    ("businessStateFull","Business State Full Name"),
    ("businessZip",      "Business Zip"),
    ("businessPhone",    "Business Phone Number"),
    ("businessEmail",    "Business Gmail"),
    ("einNumber",        "Tax ID (EIN #)"),
    ("lander_name",           "Lander Name"),
    ("representative_name",   "Representative Name"),
    ("iso_email",             "ISO Email"),
    ("ownerName",        "Owner 1 Name"),
    ("ownerDob",         "Owner 1 DOB"),
    ("ownerAddress",     "Owner 1 Home Address"),
    ("ownerCity",        "Owner 1 City"),
    ("ownerState",       "Owner 1 State"),
    ("ownerStateFull",   "Owner 1 State Full Name"),
    ("ownerZip",         "Owner 1 Zip"),
    ("ssn",              "Owner 1 SSN#"),
    ("owner2Name",       "Owner 2 Name"),
    ("owner2Dob",        "Owner 2 DOB"),
    ("owner2Address",    "Owner 2 Home Address"),
    ("owner2City",       "Owner 2 City"),
    ("owner2State",      "Owner 2 State"),
    ("owner2StateFull",  "Owner 2 State Full Name"),
    ("owner2Zip",        "Owner 2 Zip"),
    ("owner2Ssn",        "Owner 2 SSN#"),
    ("owner3Name",       "Owner 3 Name"),
    ("owner3Dob",        "Owner 3 DOB"),
    ("owner3Address",    "Owner 3 Home Address"),
    ("owner3City",       "Owner 3 City"),
    ("owner3State",      "Owner 3 State"),
    ("owner3StateFull",  "Owner 3 State Full Name"),
    ("owner3Zip",        "Owner 3 Zip"),
    ("owner3Ssn",        "Owner 3 SSN#"),
    ("owner4Name",       "Owner 4 Name"),
    ("owner4Dob",        "Owner 4 DOB"),
    ("owner4Address",    "Owner 4 Home Address"),
    ("owner4City",       "Owner 4 City"),
    ("owner4State",      "Owner 4 State"),
    ("owner4StateFull",  "Owner 4 State Full Name"),
    ("owner4Zip",        "Owner 4 Zip"),
    ("owner4Ssn",        "Owner 4 SSN#"),
]

# Available OCR mode options shown in the dropdown
OCR_MODES = ["Text PDF", "Image"]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — US State Code Lookup
# ══════════════════════════════════════════════════════════════════════════════

STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "IA", "ID",
    "IL", "IN", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN", "MO", "MS", "MT",
    "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY", "DC",
}
STATE_NAME_CODES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX",
    "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "DISTRICT OF COLUMBIA": "DC",
}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Text Utilities
# ══════════════════════════════════════════════════════════════════════════════

def clean_output(value: object) -> str:
    """Strip leading/trailing punctuation and collapse internal whitespace."""
    text = re.sub(r"\s+", " ", str(value or ""))
    text = re.sub(r"\s+([,.:;])", r"\1", text)
    return re.sub(r"^[\s:.\-]+|[\s:.\-]+$", "", text).strip()


def normalize_text(text: str) -> str:
    """Replace common encoding artifacts from PDF extraction with clean characters."""
    return (
        str(text or "")
        .replace(" ", " ")
        .replace("|", " ")
        .replace("â€œ", '"')
        .replace("â€",  '"')
        .replace("â€™", "'")
        .replace("â€˜", "'")
    )


def normalize_label(label: str) -> str:
    """
    Normalize a label for blocklist matching.
    Converts to lowercase, strips special chars, fixes known OCR typos.
    """
    normalized = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(label or "").lower())).strip()
    replacements = {
        "bus ness":       "business",
        "busi ness":      "business",
        "lega":           "legal",
        "federa":         "federal",
        "fu name":        "full name",
        "ful name":       "full name",
        "of ownersh p":   "ownership",
        "ownersh p":      "ownership",
        "state 2":        "state",
    }
    for bad, good in replacements.items():
        normalized = re.sub(rf"\b{re.escape(bad)}\b", good, normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def format_ein(value: str) -> str:
    """Return EIN as 9 digits only (strips hyphens, truncates to 9 chars)."""
    return re.sub(r"\D", "", str(value or ""))[:9]


def format_scrub_ein(value: str) -> str:
    """
    Format EIN for Credit Scrub display: ##-####### format.
    Returns error message if EIN is missing or has fewer than 9 digits.
    """
    raw    = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not raw or not digits:
        return "Tax Id is missing"
    if len(digits) < 9:
        return "Tax Id is incorrect"
    clean = digits[:9]
    return f"{clean[:2]}-{clean[2:]}"


def normalize_state_code(value: str) -> str:
    """Convert any state representation to a 2-letter state code."""
    raw_state = re.sub(r"[^A-Z0-9]", "", clean_output(value).upper())
    if raw_state in {"FI", "F1"}:   # Common OCR misread of "FL"
        return "FL"
    raw = re.sub(r"[^A-Z ]", " ", clean_output(value).upper())
    raw = re.sub(r"\s+", " ", raw).strip()
    if raw in STATE_CODES:
        return raw
    match = re.search(r"\b[A-Z]{2}\b", raw)
    if match and match.group(0) in STATE_CODES:
        return match.group(0)
    return STATE_NAME_CODES.get(raw, clean_output(value))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Lander Name Detection
# ══════════════════════════════════════════════════════════════════════════════

_ALL_LANDERS: list[str] | None = None

def _get_all_landers() -> list[str]:
    """
    Load lender names from all_lander_name.txt.
    Sorted longest-first so longer names match before shorter sub-strings.
    Cached in _ALL_LANDERS after first load.
    """
    global _ALL_LANDERS
    if _ALL_LANDERS is None:
        path = APP_DIR / "all_lander_name.txt"
        if path.exists():
            names = [
                ln.strip()
                for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip()
            ]
            names.sort(key=len, reverse=True)   # Longest match first
            _ALL_LANDERS = names
        else:
            _ALL_LANDERS = []
    return _ALL_LANDERS


def find_lander_name(raw_text: str) -> str:
    """
    Scan the raw OCR text for any known lender name.
    Uses word-boundary regex to avoid partial matches.

    Returns:
        Matched lender name (original casing) or "" if not found.
    """
    normalized = re.sub(r"\s+", " ", raw_text).lower()
    for name in _get_all_landers():
        name_lower = re.sub(r"\s+", " ", name.lower())
        if not name_lower or name_lower not in normalized:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(name_lower) + r"(?![a-z0-9])"
        if re.search(pattern, normalized):
            return name
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — Credit Scrub Builder
# ══════════════════════════════════════════════════════════════════════════════

def previous_month_name() -> str:
    """Return the name of the previous calendar month (used for revenue month default)."""
    names = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    ]
    today = date.today()
    return names[(today.month + 10) % 12]


def normalize_revenue_month(value: str) -> str:
    """Convert a month abbreviation or name to its full name."""
    aliases = {
        "jan": "January", "january": "January", "feb": "February", "february": "February",
        "febuary": "February", "mar": "March", "march": "March", "apr": "April",
        "april": "April", "may": "May", "jun": "June", "june": "June",
        "jul": "July", "july": "July", "aug": "August", "august": "August",
        "sep": "September", "sept": "September", "september": "September",
        "oct": "October", "october": "October", "nov": "November", "november": "November",
        "dec": "December", "december": "December",
    }
    return aliases.get(re.sub(r"[^a-z]", "", str(value or "").lower()), "")


def parse_pricing_scrub(text: str) -> dict[str, str]:
    """
    Parse the pricing scrub textarea input into structured fields.
    Extracts: tier, deposits, revenue month, industry, TIB, state, website.
    """
    normalized = str(text or "").replace("\r", "\n")
    lines      = [clean_output(line) for line in normalized.splitlines() if clean_output(line)]
    tier       = lines[0] if lines else ""

    # Remove any "Deposits:" portion from the tier line
    tier = re.split(r"\bDeposits?(?:\s*\([^)]*\))?\s*:", tier, maxsplit=1, flags=re.I)[0]
    if re.search(r"^(Deposits|Industry|TIB|State|Datamerch|Company\s+Website|Proceed)\b", tier, re.I):
        tier = ""

    def read(pattern: str) -> str:
        match = re.search(pattern, normalized, re.I)
        return clean_output(match.group(1)) if match else ""

    deposits_match = re.search(
        r"\bDeposits?(?:\s*\(([^)]*)\))?\s*:\s*(.*?)(?:,\s*Industry\s*:|\n|\r|$)",
        normalized, re.I
    )
    website = read(r"Company\s+Website\s*:\s*([^\n\r]*)")

    return {
        "tier":         clean_output(tier),
        "deposits":     clean_output(deposits_match.group(2)) if deposits_match else "",
        "revenueMonth": (
            normalize_revenue_month(deposits_match.group(1) if deposits_match else "")
            or previous_month_name()
        ),
        "industry":     read(r"Industry\s*:\s*([^,\n\r]*)"),
        "tib":          read(r"\bTIB\s*:\s*([^,\n\r]*)"),
        "state":        read(r"\bState\s*:\s*([^,\n\r]*)"),
        "website":      website if website and not re.search(r"^not\s+found$", website, re.I) else "Not Found",
    }


def google_search_line(name: str, state_value: str) -> str:
    """Build a Google Search label string: "Business Name ST Google Search"."""
    name = clean_output(name)
    if not name:
        return ""
    return f"{clean_output(f'{name} {normalize_state_code(state_value)}')} Google Search"


def owner_field_lines(owner_count: int, label: str) -> list[str]:
    """
    Generate per-owner field lines for the credit scrub.
    Single owner: returns [label]
    Multiple owners: returns ["FICO Owner 1 :", "FICO Owner 2 :", ...]
    """
    if owner_count <= 1:
        return [label]
    text = re.sub(r"\s*[:#]\s*$", "", str(label)).strip()
    mark = "#" if str(label).strip().endswith("#") else ":"
    return [f"{text} Owner {i} {mark}" for i in range(1, owner_count + 1)]


def build_credit_scrub(values: dict[str, str], pricing_text: str, received: bool = False) -> str:
    """
    Build the Credit Scrub text block from form values and pricing input.

    The credit scrub includes:
      - Business and owner Google Search links
      - Company website
      - FICO placeholders
      - Revenue month and deposits
      - Industry, TIB, State
      - Datamerch status
      - Received flag
      - EIN (formatted as ##-#######)
      - Customer ID placeholders

    Args:
        values:       Frontend field values dict.
        pricing_text: Raw text from the Pricing Scrub textarea.
        received:     Whether the "Received" checkbox is checked.

    Returns:
        Multiline credit scrub text string.
    """
    pricing    = parse_pricing_scrub(pricing_text)
    scrub_state = values.get("businessState") or pricing.get("state", "")
    lines: list[str] = []

    if pricing["tier"]:
        lines.extend([pricing["tier"], ""])

    business_search = google_search_line(
        values.get("businessName") or values.get("businessDbaName", ""),
        scrub_state
    )
    people = [
        google_search_line(values.get(key, ""), scrub_state)
        for key in ["ownerName", "owner2Name", "owner3Name", "owner4Name"]
    ]
    people = [line for line in people if line]

    owner_count = max(1, sum(
        1 for key in ["ownerName", "owner2Name", "owner3Name", "owner4Name"]
        if clean_output(values.get(key, ""))
    ))

    if business_search:
        lines.append(business_search)
    if business_search and people:
        lines.append("")
    lines.extend(people)
    if business_search or people:
        lines.append("")

    lines.extend([
        f"Company Website : {pricing['website'] or 'Not Found'}",
        "",
        *owner_field_lines(owner_count, "FICO :"),
        f"{pricing['revenueMonth']} Revenue : {pricing['deposits']}",
        f"Industry : {pricing['industry']}",
        f"TIB : {pricing['tib']}",
        f"State : {normalize_state_code(scrub_state)}",
        "Datamerch : No Match",
        f"Received : {'Y' if received else 'N'}",
        f"EIN : {format_scrub_ein(values.get('einNumber', ''))}",
        *owner_field_lines(owner_count, "Customer Id #"),
    ])

    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).rstrip()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — Raw Text Cleaning (Display Version)
# ══════════════════════════════════════════════════════════════════════════════

def load_raw_text_remove_rules() -> tuple[set[str], list[str], list[str]]:
    """Load blocklist rules from raw_text_remove_lines.txt."""
    exact: set[str]    = set()
    contains: list[str] = []
    regexes: list[str]  = []
    if not RAW_TEXT_BLOCKLIST_PATH.exists():
        return exact, contains, regexes
    for raw_line in RAW_TEXT_BLOCKLIST_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("contains:"):
            contains.append(normalize_label(line.split(":", 1)[1]))
        elif line.lower().startswith("regex:"):
            regexes.append(line.split(":", 1)[1].strip())
        else:
            exact.add(normalize_label(line))
    return exact, contains, regexes


def should_remove_raw_text_line(
    line: str,
    exact: set[str],
    contains: list[str],
    regexes: list[str],
) -> bool:
    """Return True if this line should be excluded from the displayed raw text."""
    text       = clean_output(line)
    normalized = normalize_label(text)
    if not text:                                               return True
    if RAW_TEXT_MAX_LINE_LENGTH > 0 and len(text) > RAW_TEXT_MAX_LINE_LENGTH: return True
    if re.fullmatch(r"[YNyn]", text) or re.fullmatch(r"\d", text): return True
    if re.fullmatch(r"[-_*/\\|.,;:()\[\]{}]+", text):             return True
    if normalized in exact:                                     return True
    if any(marker and marker in normalized for marker in contains): return True
    for pattern in regexes:
        try:
            if re.search(pattern, text, re.I):
                return True
        except re.error:
            continue
    return False


def clean_raw_ocr_text_for_display(text: str) -> str:
    """
    Clean raw OCR text for display in the UI textarea.
    Uses the same blocklist as text_cleaner but with the display-specific
    MAX_LINE_LENGTH setting (typically much higher than the AI limit).
    """
    exact, contains, regexes = load_raw_text_remove_rules()
    lines = []
    for line in normalize_text(text).replace("\r", "\n").splitlines():
        cleaned = clean_output(line)
        if should_remove_raw_text_line(cleaned, exact, contains, regexes):
            continue
        lines.append(cleaned)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def raw_text_lines(text: str) -> list[str]:
    """Split text into non-empty cleaned lines."""
    return [
        clean_output(line)
        for line in normalize_text(text).replace("\r", "\n").splitlines()
        if clean_output(line)
    ]


def merge_text_sources(*chunks: str) -> str:
    """
    Merge multiple OCR text chunks, deduplicating lines that appear in
    earlier chunks. Used to combine text layer + layout extraction + widget text.
    """
    lines: list[str]          = []
    seen_previous_chunks: set[str] = set()
    for chunk in chunks:
        chunk_lines = raw_text_lines(chunk)
        if lines and chunk_lines:
            lines.append("")
        seen_this_chunk: set[str] = set()
        for line in chunk_lines:
            key = normalize_label(line)
            if not key or key in seen_previous_chunks:
                continue
            seen_this_chunk.add(key)
            lines.append(line)
        seen_previous_chunks.update(seen_this_chunk)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def text_layer_has_enough_values(text: str) -> bool:
    """
    Return True if the PDF text layer contains enough data to skip OCR.
    Checks for minimum length and presence of form-related keywords.
    """
    normalized = normalize_label(text)
    labels = ["business", "owner", "address", "city", "state", "zip", "tax", "ssn"]
    return len(clean_output(text)) > 250 and sum(1 for l in labels if l in normalized) >= 4


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — OCR Pipeline
# ══════════════════════════════════════════════════════════════════════════════

class OCRPipeline:
    """
    Handles OCR extraction from PDF files and images.

    Extraction modes:
      - PDF with text layer: Extract text directly via PyMuPDF (no OCR needed)
      - PDF without text layer / Image: Use PaddleOCR (requires optional install)

    Image preprocessing (when cv2 is available):
      - Grayscale conversion
      - Denoising
      - 1.6x upscaling
      - Adaptive thresholding for better contrast
    """

    def __init__(self, progress) -> None:
        self.progress   = progress       # Callable for status messages
        self.temp_files: list[Path] = []  # Track temp files for cleanup

    def temp_path(self, prefix: str, suffix: str) -> Path:
        """Create a named temp file and register it for cleanup."""
        fd, filename = tempfile.mkstemp(prefix=prefix, suffix=suffix)
        os.close(fd)
        path = Path(filename)
        self.temp_files.append(path)
        return path

    def cleanup(self) -> None:
        """Delete all temporary files created during this pipeline run."""
        for path in self.temp_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self.temp_files.clear()

    def preprocess_image(self, image_path: Path) -> Path:
        """
        Apply image preprocessing to improve OCR accuracy.
        Returns original path if cv2 (OpenCV) is not installed.
        """
        try:
            cv2 = importlib.import_module("cv2")
        except Exception:
            return image_path   # Silently skip preprocessing if cv2 not installed

        image = cv2.imread(str(image_path))
        if image is None:
            return image_path

        gray   = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gray   = cv2.fastNlMeansDenoising(gray, h=15)
        gray   = cv2.resize(gray, None, fx=1.6, fy=1.6, interpolation=cv2.INTER_CUBIC)
        binary = cv2.adaptiveThreshold(
            gray, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 31, 9
        )
        output = self.temp_path("ocr_preprocessed_", ".png")
        cv2.imwrite(str(output), binary)
        return output

    def images_from_pdf(self, pdf_path: Path) -> list[Path]:
        """
        Rasterize each PDF page to a PNG image for OCR processing.
        Uses 2.4x scaling for high-resolution output.
        """
        try:
            fitz = importlib.import_module("fitz")
        except Exception as exc:
            raise RuntimeError("PDF OCR requires PyMuPDF: pip install pymupdf") from exc

        doc   = fitz.open(str(pdf_path))
        paths: list[Path] = []
        try:
            for page_index, page in enumerate(doc, start=1):
                self.progress("Processing...")
                pixmap     = page.get_pixmap(matrix=fitz.Matrix(2.4, 2.4), alpha=False)
                image_path = self.temp_path(f"pdf_page_{page_index}_", ".png")
                pixmap.save(str(image_path))
                paths.append(image_path)
        finally:
            doc.close()
        return paths

    def fitz_page_layout_text(self, page) -> str:
        """
        Extract text from a PDF page preserving reading order.
        Groups words by block/line, sorts by vertical then horizontal position.
        """
        try:
            words = page.get_text("words", sort=True)
        except Exception:
            return ""

        rows: dict[tuple[int, int], list[tuple]] = {}
        for word in words or []:
            if len(word) < 5 or not clean_output(word[4]):
                continue
            block = int(word[5]) if len(word) > 5 else 0
            line  = int(word[6]) if len(word) > 6 else len(rows)
            rows.setdefault((block, line), []).append(word)

        ordered: list[tuple[float, float, str]] = []
        for row_words in rows.values():
            row_words.sort(key=lambda w: (float(w[0]), float(w[1])))
            line_text = clean_output(" ".join(str(w[4]) for w in row_words))
            if line_text:
                ordered.append((
                    min(float(w[1]) for w in row_words),
                    min(float(w[0]) for w in row_words),
                    line_text,
                ))
        ordered.sort(key=lambda item: (round(item[0], 1), item[1]))
        return "\n".join(item[2] for item in ordered)

    def fitz_widget_text(self, doc) -> str:
        """Extract filled form field values from PDF widgets (AcroForm fields)."""
        lines: list[str] = []
        for page in doc:
            try:
                widgets = list(page.widgets() or [])
            except Exception:
                widgets = []
            for widget in widgets:
                value = clean_output(getattr(widget, "field_value", ""))
                if not value or value.lower() in {"off", "false", "none"}:
                    continue
                name = clean_output(getattr(widget, "field_name", ""))
                lines.append(f"{name}: {value}" if name else value)
        return "\n".join(lines)

    def pypdf_form_text(self, pdf_path: Path) -> str:
        """
        Extract form field values using pypdf as a fallback to fitz widgets.
        Supports both pypdf and PyPDF2 package names.
        """
        try:
            try:
                pypdf = importlib.import_module("pypdf")
            except Exception:
                pypdf = importlib.import_module("PyPDF2")
            reader = pypdf.PdfReader(str(pdf_path))
            fields = reader.get_fields() or {}
        except Exception:
            return ""

        lines: list[str] = []
        for name, field in fields.items():
            value = ""
            if isinstance(field, dict):
                value = clean_output(field.get("/V") or field.get("value") or "")
            if value and value.lower() not in {"off", "false", "none"}:
                lines.append(f"{clean_output(name)}: {value}")
        return "\n".join(lines)

    def pdf_text_layer(self, pdf_path: Path) -> str:
        """
        Extract all text from a PDF using multiple PyMuPDF strategies:
          1. Plain text extraction (get_text "text")
          2. Layout-aware word extraction (get_text "words")
          3. Widget/form field values
          4. pypdf form fields (fallback)
        Results are merged and deduplicated.
        """
        try:
            fitz = importlib.import_module("fitz")
        except Exception:
            return ""
        try:
            doc         = fitz.open(str(pdf_path))
            layout_text = "\n\n".join(self.fitz_page_layout_text(page) for page in doc)
            plain_text  = "\n\n".join(page.get_text("text") for page in doc)
            widget_text = self.fitz_widget_text(doc)
            doc.close()
            return merge_text_sources(plain_text, layout_text, widget_text, self.pypdf_form_text(pdf_path))
        except Exception:
            return ""

    def input_images(self, file_path: Path) -> tuple[str, list[Path]]:
        """
        Prepare input for OCR:
          - PDF → extract text layer + rasterize each page to image
          - Image → return as-is
        """
        if file_path.suffix.lower() == ".pdf":
            text_layer = self.pdf_text_layer(file_path)
            return text_layer, self.images_from_pdf(file_path)
        return "", [file_path]

    def run(self, file_path: Path, engine: str) -> str:
        """
        Main OCR entry point.
        Returns merged text from the PDF text layer + PaddleOCR (if needed).
        """
        text_layer, image_paths = self.input_images(file_path)
        mode = engine if engine in OCR_MODES else "Image"

        # If the PDF has a good text layer, use it directly — skip image OCR
        if file_path.suffix.lower() == ".pdf" and text_layer:
            return text_layer

        chunks = [text_layer] if text_layer else []
        self.progress("Processing...")
        result = self.paddle(image_paths)
        if clean_output(result):
            chunks.append(result)
        return merge_text_sources(*chunks)

    def paddle(self, image_paths: list[Path]) -> str:
        """
        Run PaddleOCR on a list of image files.
        Requires: pip install paddleocr paddlepaddle
        Raises RuntimeError with a user-friendly message if not installed.
        """
        try:
            paddleocr = importlib.import_module("paddle" + "ocr")   # Split to avoid static analysis issues
        except Exception as exc:
            raise RuntimeError(
                "Image / handwriting PDFs require PaddleOCR.\n"
                "Install: pip install paddleocr paddlepaddle\n"
                "Or process the document manually."
            ) from exc

        ocr   = paddleocr.PaddleOCR(use_angle_cls=True, lang="en")
        lines = []
        for image_path in image_paths:
            image_file = str(self.preprocess_image(image_path))
            try:
                result = ocr.ocr(image_file, cls=True)
            except TypeError as exc:
                if "cls" not in str(exc):
                    raise
                result = ocr.ocr(image_file)   # Older PaddleOCR API (no cls arg)
            for page in result or []:
                for item in page or []:
                    try:
                        lines.append(item[1][0])
                    except Exception:
                        pass
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — HTML Page Template
# ══════════════════════════════════════════════════════════════════════════════

# The full HTML/CSS/JS for the single-page app is defined inline.
# Template placeholders are replaced at runtime:
#   __FIELD_GROUPS__     → JSON array of field group definitions
#   __FIXED_CONTACT__    → JSON object of fixed business contact values
#   __OCR_MODE_OPTIONS__ → <option> HTML elements for the OCR mode dropdown
HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hand Writing Credit App</title>
  <style>
    /* ── CSS Custom Properties ─────────────────────────────────────────────── */
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #657283;
      --line: #d9dee7;
      --accent: #0f766e;      /* Teal — primary action color */
      --accent-2: #1d4ed8;    /* Blue — secondary action color */
      --warn: #b45309;
      --shadow: 0 18px 48px rgba(25, 35, 55, 0.10);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    button, input, textarea, select { font: inherit; }

    /* ── Layout ─────────────────────────────────────────────────────────────── */
    .shell { width: min(1560px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 36px; }
    header { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 22px; }
    h1 { margin: 0; font-size: clamp(24px, 3vw, 36px); line-height: 1.08; }
    .subtle { margin: 7px 0 0; color: var(--muted); font-size: 14px; }
    .status {
      min-width: 250px; padding: 12px 14px; border: 1px solid var(--line); border-radius: 8px;
      background: var(--panel); color: var(--muted); font-size: 13px; text-align: right;
      box-shadow: 0 8px 20px rgba(25, 35, 55, 0.06);
    }

    /* ── Page Views ─────────────────────────────────────────────────────────── */
    .page { display: none; }
    .page.active { display: grid; }
    .start-layout, .final-layout { grid-template-columns: minmax(300px, 360px) minmax(0, 1fr); gap: 18px; align-items: start; }
    .final-layout.scrub-only { grid-template-columns: minmax(300px, 760px); }
    .final-layout.scrub-only .extractor-column { display: none; }
    .final-layout.scrub-only .scrub-column { position: static; }
    .scrub-column { display: grid; gap: 18px; position: sticky; top: 20px; align-self: start; }
    .extractor-column { display: grid; grid-template-columns: minmax(280px, 320px) minmax(0, 1fr); gap: 18px; align-items: start; min-width: 0; }

    /* ── Panels & Sections ─────────────────────────────────────────────────── */
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); }
    .upload-panel, .scrub-panel, .section { padding: 18px; }
    .upload-panel { position: sticky; top: 20px; }

    /* ── Dropzone ───────────────────────────────────────────────────────────── */
    .dropzone {
      display: grid; place-items: center; min-height: 210px; border: 2px dashed #a8b2c1; border-radius: 8px;
      background: #fbfcfe; text-align: center; cursor: pointer; transition: border-color 160ms ease, background 160ms ease;
    }
    .dropzone.dragging { border-color: var(--accent); background: #ecfdf5; }
    .dropzone input { position: absolute; inline-size: 1px; block-size: 1px; overflow: hidden; clip: rect(0 0 0 0); }
    .drop-mark {
      width: 52px; height: 52px; margin: 0 auto 14px; border-radius: 8px; display: grid; place-items: center;
      background: #e7f4f2; color: var(--accent); font-weight: 800; font-size: 24px;
    }
    .drop-title { margin: 0; font-size: 16px; font-weight: 750; }
    .drop-copy { margin: 8px 0 0; color: var(--muted); font-size: 13px; line-height: 1.45; overflow-wrap: anywhere; }

    /* ── Buttons ────────────────────────────────────────────────────────────── */
    .actions { display: grid; grid-template-columns: 1fr; gap: 10px; margin-top: 16px; }
    .btn { min-height: 42px; border: 0; border-radius: 8px; padding: 10px 14px; color: #fff; background: var(--accent); font-weight: 750; cursor: pointer; }
    .btn.secondary { background: var(--accent-2); }
    .btn.ghost { background: #eef2f7; color: var(--ink); }
    .btn:disabled { cursor: not-allowed; opacity: 0.55; }
    .engine { width: 100%; min-height: 38px; border: 1px solid var(--line); border-radius: 8px; padding: 8px; background: #fff; }

    /* ── Form Fields ────────────────────────────────────────────────────────── */
    .meta { display: grid; gap: 8px; margin-top: 16px; color: var(--muted); font-size: 13px; line-height: 1.45; }
    .content { display: grid; gap: 18px; min-width: 0; }
    .section-head { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 14px; }
    h2 { margin: 0; font-size: 17px; }
    .confidence { color: var(--muted); font-size: 12px; white-space: nowrap; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .field { display: grid; gap: 6px; min-width: 0; }
    .field span { color: #344055; font-size: 12px; font-weight: 750; }
    .field-control { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: center; }
    .field input {
      width: 100%; min-height: 40px; border: 1px solid var(--line); border-radius: 8px; padding: 9px 10px;
      color: var(--ink); background: #fff; outline: none;
    }
    .field input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(15, 118, 110, 0.13); }
    textarea {
      width: 100%; min-height: 240px; resize: vertical; border: 1px solid var(--line); border-radius: 8px;
      padding: 12px; color: #263245; background: #fbfcfe; line-height: 1.45; outline: none;
    }

    /* ── Credit Scrub Section ───────────────────────────────────────────────── */
    .scrub-textarea { min-height: 230px; font-family: Consolas, "SFMono-Regular", monospace; font-size: 13px; }
    .scrub-output {
      width: 100%; min-height: 420px; border: 1px solid var(--line); border-radius: 8px; padding: 14px;
      background: #fff; color: var(--ink); font-size: 16px; line-height: 1.45; outline: none; overflow: auto;
      user-select: text; white-space: pre-wrap;
    }
    .scrub-output a { color: #1a0dab; text-decoration: underline; }
    .scrub-output div { min-height: 20px; }
    .copy-actions { display: flex; align-items: center; gap: 10px; }
    .received-toggle {
      display: inline-flex; align-items: center; gap: 6px; color: var(--ink);
      font-size: 12px; font-weight: 650; cursor: pointer; user-select: none;
    }
    .received-toggle input { width: 14px; height: 14px; margin: 0; accent-color: var(--accent-2); }
    .mini-btn {
      min-height: 32px; border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px;
      background: #eef2f7; color: var(--ink); font-size: 12px; font-weight: 750; cursor: pointer;
    }
    .field-copy-btn { min-height: 40px; min-width: 58px; }

    /* ── Progress / Loading ─────────────────────────────────────────────────── */
    progress { width: 100%; height: 10px; accent-color: var(--accent); }
    .progress-wrap {
      position: fixed; inset: 0; z-index: 50; display: none; place-items: center; padding: 22px;
      background: rgba(248, 250, 252, 0.58); backdrop-filter: blur(7px);
    }
    .progress-wrap.active { display: grid; }
    .loading-card {
      width: min(360px, 100%); display: grid; justify-items: center; gap: 14px; padding: 28px 26px;
      border: 1px solid rgba(203, 213, 225, 0.85); border-radius: 8px; background: rgba(255,255,255,0.92);
      box-shadow: 0 24px 70px rgba(25, 35, 55, 0.22);
    }
    .loading-spinner {
      width: 64px; height: 64px; border-radius: 50%; border: 5px solid #d8e2ee;
      border-top-color: var(--accent); border-right-color: var(--accent-2);
      animation: loadingSpin 850ms linear infinite;
    }
    .loader-text { margin: 0; color: var(--ink); font-size: 15px; font-weight: 750; text-align: center; }
    @keyframes loadingSpin { to { transform: rotate(360deg); } }

    /* ── Status Modal ───────────────────────────────────────────────────────── */
    .status-modal-wrap {
      position: fixed; inset: 0; z-index: 60; display: none; place-items: center; padding: 22px;
      background: rgba(15, 20, 35, 0.55); backdrop-filter: blur(6px);
    }
    .status-modal-wrap.active { display: grid; }
    .status-modal {
      width: min(400px, 100%); display: grid; gap: 20px; padding: 32px 28px;
      border: 1px solid rgba(203, 213, 225, 0.85); border-radius: 12px;
      background: #ffffff; box-shadow: 0 24px 70px rgba(25, 35, 55, 0.28); text-align: center;
    }
    .status-modal h3 { margin: 0; font-size: 20px; font-weight: 800; color: var(--ink); }
    .status-modal p  { margin: 0; color: var(--muted); font-size: 14px; line-height: 1.5; }
    .status-modal-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .btn-proceed  { min-height: 46px; border: 0; border-radius: 8px; padding: 12px 16px; color: #fff; background: #0f766e; font-weight: 750; font-size: 15px; cursor: pointer; }
    .btn-declined { min-height: 46px; border: 0; border-radius: 8px; padding: 12px 16px; color: #fff; background: #dc2626; font-weight: 750; font-size: 15px; cursor: pointer; }

    /* ── Misc ───────────────────────────────────────────────────────────────── */
    .warn { color: var(--warn); font-size: 13px; line-height: 1.45; }
    .raw-data-panel { min-height: 640px; }
    .raw-textarea { min-height: 560px; font-family: Consolas, "SFMono-Regular", monospace; font-size: 13px; white-space: pre; overflow: auto; }
    .final-summary { display: grid; gap: 8px; min-height: 118px; border: 1px solid var(--line); border-radius: 8px; padding: 14px; background: #fbfcfe; }
    .summary-label { margin: 0; color: var(--muted); font-size: 12px; font-weight: 750; text-transform: uppercase; }
    .summary-file  { margin: 0; color: var(--ink); font-weight: 750; overflow-wrap: anywhere; }

    /* ── Responsive ─────────────────────────────────────────────────────────── */
    @media (max-width: 1180px) {
      .start-layout, .final-layout { grid-template-columns: 1fr; }
      .scrub-column, .upload-panel { position: static; }
    }
    @media (max-width: 860px) {
      header { display: grid; }
      .status { text-align: left; min-width: 0; }
      .extractor-column { grid-template-columns: 1fr; }
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>

  <!-- ── Application Status Modal (Proceed / Declined) ─────────────────────── -->
  <div class="status-modal-wrap" id="statusModalWrap">
    <div class="status-modal">
      <h3>Application Status</h3>
      <p>Select the status for this application before saving to Google Sheet.</p>
      <div class="status-modal-actions">
        <button class="btn-declined" id="declinedBtn" type="button">Declined &amp; Received on</button>
        <button class="btn-proceed"  id="proceedBtn"  type="button">Proceed</button>
      </div>
    </div>
  </div>

  <!-- ── Proceed Confirmation Modal ───────────────────────────────────────── -->
  <div class="status-modal-wrap" id="confirmModalWrap">
    <div class="status-modal">
      <h3>Are you sure?</h3>
      <p>You selected <strong>Proceed</strong>. Do you want to confirm and save to Google Sheet?</p>
      <div class="status-modal-actions">
        <button class="btn-declined" id="confirmBackBtn" type="button">Back</button>
        <button class="btn-proceed"  id="confirmYesBtn"  type="button">Yes, Proceed</button>
      </div>
    </div>
  </div>

  <div class="shell">
    <header>
      <div>
        <h1>D ANALYTICAL CREDIT FORM</h1>
        <p class="subtle">Upload a PDF or image to extract the required business and owner details.</p>
      </div>
      <div class="status" id="status">Ready</div>
    </header>

    <!-- ── Page 1: Upload & Raw Text ──────────────────────────────────────── -->
    <main class="page start-layout active" id="extractPage">
      <aside class="panel upload-panel">
        <label class="dropzone" id="dropzone">
          <input id="fileInput" type="file" accept="application/pdf,image/*">
          <div>
            <div class="drop-mark">+</div>
            <p class="drop-title">select PDF / Image</p>
            <p class="drop-copy" id="fileName">Drag-drop &amp; select your file</p>
          </div>
        </label>
        <div style="margin-top:12px">
          <select class="engine" id="engine">
            __OCR_MODE_OPTIONS__
          </select>
        </div>
        <div class="progress-wrap" id="progressWrap" aria-live="polite" aria-busy="false">
          <div class="loading-card" role="status">
            <div class="loading-spinner" aria-hidden="true"></div>
            <p class="loader-text" id="progressText">Processing...</p>
            <progress id="progress" value="0" max="100"></progress>
          </div>
        </div>
        <div class="actions">
          <button class="btn"           id="extractBtn"   disabled>Extract PDF Data</button>
          <button class="btn secondary" id="scrubOnlyBtn">Only for scrub</button>
          <button class="btn secondary" id="sheetClearBtn" type="button">Sheet Clear</button>
          <button class="btn ghost"     id="clearBtn">Clear</button>
        </div>
        <div class="meta">
          <div>For best results, use a clear, straight, high-resolution scan or image.</div>
        </div>
      </aside>
      <section class="panel section raw-data-panel">
        <div class="section-head">
          <h2>Extracted PDF Data</h2>
          <div class="confidence" id="rawCount">0 characters</div>
        </div>
        <textarea class="raw-textarea" id="rawText" readonly spellcheck="false" wrap="off" placeholder="Raw Data."></textarea>
      </section>
    </main>

    <!-- ── Page 2: Structured Fields + Credit Scrub ───────────────────────── -->
    <main class="page final-layout" id="finalPage">
      <aside class="scrub-column">
        <section class="panel scrub-panel">
          <div class="section-head"><h2>Pricing Scrub</h2></div>
          <textarea class="scrub-textarea" id="pricingScrub" spellcheck="false"></textarea>
        </section>
        <section class="panel scrub-panel">
          <div class="section-head">
            <h2>Credit Scrub</h2>
            <div class="copy-actions">
              <label class="received-toggle">
                <input id="receivedCheck" type="checkbox">
                Received
              </label>
              <button class="mini-btn" id="copyCreditBtn" type="button">Copy</button>
            </div>
          </div>
          <div class="scrub-output" id="creditScrub" tabindex="0"></div>
        </section>
      </aside>
      <section class="extractor-column">
        <aside class="panel upload-panel">
          <div class="final-summary">
            <p class="summary-label">Filtered From</p>
            <p class="summary-file" id="finalFileName">No file selected</p>
            <p class="drop-copy"   id="finalSummary">Required business and owner fields are ready.</p>
          </div>
          <div class="actions">
            <button class="btn ghost" id="backBtn">Back to Extracted Data</button>
            <button class="btn ghost" id="clearFinalBtn">Clear</button>
          </div>
          <div class="meta">
            <div>Review the filtered fields before submitting or downloading.</div>
            <div class="warn">Please verify EIN and SSN values before downloading the Excel file.</div>
          </div>
        </aside>
        <section class="content" id="content"></section>
      </section>
    </main>
  </div>

  <script>
    // ── Constants injected from Python server ─────────────────────────────────
    const fieldGroups        = __FIELD_GROUPS__;
    const fixedBusinessContact = __FIXED_CONTACT__;

    // ── Application state ─────────────────────────────────────────────────────
    const state = { file: null, values: {}, rawText: "", view: "extract" };
    fieldGroups.flatMap(g => g.fields).forEach(([key]) => state.values[key] = "");
    Object.assign(state.values, fixedBusinessContact);

    // ── DOM references ────────────────────────────────────────────────────────
    const $ = (id) => document.getElementById(id);
    const statusEl      = $("status");
    const extractPage   = $("extractPage");
    const finalPage     = $("finalPage");
    const fileInput     = $("fileInput");
    const fileName      = $("fileName");
    const extractBtn    = $("extractBtn");
    const scrubOnlyBtn  = $("scrubOnlyBtn");
    const sheetClearBtn = $("sheetClearBtn");
    const clearBtn      = $("clearBtn");
    const clearFinalBtn = $("clearFinalBtn");
    const backBtn       = $("backBtn");
    const pricingScrub  = $("pricingScrub");
    const creditScrub   = $("creditScrub");
    const receivedCheck = $("receivedCheck");
    const rawText       = $("rawText");
    const rawCount      = $("rawCount");
    const finalFileName = $("finalFileName");
    const finalSummary  = $("finalSummary");
    const progressWrap  = $("progressWrap");
    const progress      = $("progress");
    const progressText  = $("progressText");

    // ── State code lookups ────────────────────────────────────────────────────
    const stateCodes = new Set([
      "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","IA","ID","IL","IN","KS",
      "KY","LA","MA","MD","ME","MI","MN","MO","MS","MT","NC","ND","NE","NH","NJ","NM",
      "NV","NY","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VA","VT","WA","WI",
      "WV","WY","DC"
    ]);
    const stateNameCodes = {
      ALABAMA:"AL",ALASKA:"AK",ARIZONA:"AZ",ARKANSAS:"AR",CALIFORNIA:"CA",COLORADO:"CO",
      CONNECTICUT:"CT",DELAWARE:"DE",FLORIDA:"FL",GEORGIA:"GA",HAWAII:"HI",IDAHO:"ID",
      ILLINOIS:"IL",INDIANA:"IN",IOWA:"IA",KANSAS:"KS",KENTUCKY:"KY",LOUISIANA:"LA",
      MAINE:"ME",MARYLAND:"MD",MASSACHUSETTS:"MA",MICHIGAN:"MI",MINNESOTA:"MN",
      MISSISSIPPI:"MS",MISSOURI:"MO",MONTANA:"MT",NEBRASKA:"NE",NEVADA:"NV",
      "NEW HAMPSHIRE":"NH","NEW JERSEY":"NJ","NEW MEXICO":"NM","NEW YORK":"NY",
      "NORTH CAROLINA":"NC","NORTH DAKOTA":"ND",OHIO:"OH",OKLAHOMA:"OK",OREGON:"OR",
      PENNSYLVANIA:"PA","RHODE ISLAND":"RI","SOUTH CAROLINA":"SC","SOUTH DAKOTA":"SD",
      TENNESSEE:"TN",TEXAS:"TX",UTAH:"UT",VERMONT:"VT",VIRGINIA:"VA",WASHINGTON:"WA",
      "WEST VIRGINIA":"WV",WISCONSIN:"WI",WYOMING:"WY","DISTRICT OF COLUMBIA":"DC"
    };
    const stateFullNames   = Object.fromEntries(Object.entries(stateNameCodes).map(([n,c]) => [c,n]));
    const computedFields   = new Set(["businessStateFull","ownerStateFull","owner2StateFull","owner3StateFull","owner4StateFull"]);
    const stateFullKeys    = { businessState:"businessStateFull", ownerState:"ownerStateFull", owner2State:"owner2StateFull", owner3State:"owner3StateFull", owner4State:"owner4StateFull" };

    // ── UI Helpers ────────────────────────────────────────────────────────────
    function setStatus(text) { statusEl.textContent = text; }

    function setProgress(active, value = 0, text = "Processing...") {
      progressWrap.classList.toggle("active", active);
      progressWrap.setAttribute("aria-busy", active ? "true" : "false");
      progress.value      = value;
      progressText.textContent = text;
    }

    function setView(view) {
      state.view = view;
      extractPage.classList.toggle("active", view === "extract");
      finalPage.classList.toggle("active",   view === "final");
      window.scrollTo({ top: 0, behavior: "smooth" });
    }

    function setScrubOnly(active) { finalPage.classList.toggle("scrub-only", active); }

    function resetValues() {
      fieldGroups.flatMap(g => g.fields).forEach(([key]) => state.values[key] = "");
      Object.assign(state.values, fixedBusinessContact);
    }

    function escapeHtml(value) {
      return String(value || "").replace(/[&<>"']/g, ch =>
        ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[ch])
      );
    }

    // ── State normalization helpers ───────────────────────────────────────────
    function clean(value) {
      return String(value || "").replace(/ /g," ").replace(/[ \t]+/g," ").replace(/\s+([,:])/g,"$1").trim();
    }

    function normalizeStateCode(value) {
      const rawState = clean(value).toUpperCase().replace(/[^A-Z0-9]/g,"");
      if (rawState === "FI" || rawState === "F1") return "FL";
      const cleaned = clean(value).replace(/[^A-Za-z ]/g," ").replace(/\s+/g," ").trim();
      if (!cleaned) return "";
      const upper = cleaned.toUpperCase();
      if (stateCodes.has(upper)) return upper;
      const m = upper.match(/\b[A-Z]{2}\b/);
      if (m && stateCodes.has(m[0])) return m[0];
      return stateNameCodes[upper] || cleaned;
    }

    function stateFullName(value) { return stateFullNames[normalizeStateCode(value)] || ""; }

    function syncStateFullValue(stateKey) {
      const fullKey = stateFullKeys[stateKey];
      if (!fullKey) return;
      state.values[fullKey] = stateFullName(state.values[stateKey] || "");
      const input = document.querySelector(`[data-key="${fullKey}"]`);
      if (input) input.value = state.values[fullKey];
    }

    // ── AI Response Application ───────────────────────────────────────────────
    function applyAiResponse(data) {
      resetValues();
      const business = data.business_info || {};
      Object.assign(state.values, {
        businessName:     business.business_name || "",
        businessDbaName:  business.dba_name      || "",
        businessAddress:  business.business_street|| "",
        businessCity:     business.business_city  || "",
        businessState:    business.business_state || "",
        businessStateFull: stateFullName(business.business_state || ""),
        businessZip:      business.business_zip   || "",
        businessPhone:    business.business_phone || fixedBusinessContact.businessPhone || "",
        businessEmail:    business.business_email || fixedBusinessContact.businessEmail || "",
        einNumber:        business.ein_no         || "",
      });
      const ownerKeys = [
        { name:"ownerName",  dob:"ownerDob",  address:"ownerAddress",  city:"ownerCity",  state:"ownerState",  stateFull:"ownerStateFull",  zip:"ownerZip",  ssn:"ssn" },
        { name:"owner2Name", dob:"owner2Dob", address:"owner2Address", city:"owner2City", state:"owner2State", stateFull:"owner2StateFull", zip:"owner2Zip", ssn:"owner2Ssn" },
        { name:"owner3Name", dob:"owner3Dob", address:"owner3Address", city:"owner3City", state:"owner3State", stateFull:"owner3StateFull", zip:"owner3Zip", ssn:"owner3Ssn" },
        { name:"owner4Name", dob:"owner4Dob", address:"owner4Address", city:"owner4City", state:"owner4State", stateFull:"owner4StateFull", zip:"owner4Zip", ssn:"owner4Ssn" },
      ];
      (data.owners || []).slice(0, 4).forEach((owner, i) => {
        const k = ownerKeys[i];
        state.values[k.name]      = owner.owner_name   || "";
        state.values[k.dob]       = owner.owner_dob    || "";
        state.values[k.address]   = owner.owner_street || "";
        state.values[k.city]      = owner.owner_city   || "";
        state.values[k.state]     = owner.owner_state  || "";
        state.values[k.stateFull] = stateFullName(owner.owner_state || "");
        state.values[k.zip]       = owner.owner_zip    || "";
        state.values[k.ssn]       = owner.owner_ssn    || "";
      });
      Object.assign(state.values, fixedBusinessContact);
      state.values.lander_name        = data.lander_name        || "";
      state.values.representative_name = data.representative_name || "";
      state.values.iso_email          = data.iso_email           || "";
    }

    function setRawText(text = "") {
      state.rawText         = text;
      rawText.value         = text;
      rawCount.textContent  = `${text.length.toLocaleString()} characters`;
    }

    // ── Field Rendering ───────────────────────────────────────────────────────
    function renderFields() {
      Object.assign(state.values, fixedBusinessContact);
      const content = $("content");
      content.innerHTML = "";
      fieldGroups.forEach(group => {
        const panel = document.createElement("div");
        panel.className = "panel section";
        const found = group.fields.filter(([key]) => state.values[key]).length;
        panel.innerHTML = `<div class="section-head"><h2>${escapeHtml(group.name)}</h2><div class="confidence">${found} fields found</div></div><div class="grid"></div>`;
        const grid = panel.querySelector(".grid");
        group.fields.forEach(([key, label]) => {
          const field  = document.createElement("div");
          field.className = "field";
          const locked = Object.prototype.hasOwnProperty.call(fixedBusinessContact, key) || computedFields.has(key);
          field.innerHTML = `<span>${escapeHtml(label)}</span><div class="field-control"><input data-key="${key}" aria-label="${escapeHtml(label)}" autocomplete="off" value="${escapeHtml(state.values[key] || "")}" ${locked ? "readonly" : ""}><button class="mini-btn field-copy-btn" type="button" data-copy-key="${key}" title="Copy ${escapeHtml(label)}">Copy</button></div>`;
          const input = field.querySelector("input");
          field.querySelector("[data-copy-key]").addEventListener("click", ev => {
            copyFieldValue(key, label, input, ev.currentTarget);
          });
          input.addEventListener("input", ev => {
            if (locked) { state.values[key] = fixedBusinessContact[key]; ev.target.value = fixedBusinessContact[key]; return; }
            state.values[key] = ev.target.value.trim();
            syncStateFullValue(key);
            updateButtons();
            resetReceivedAndUpdate();
          });
          grid.appendChild(field);
        });
        content.appendChild(panel);
      });
      updateButtons();
      updateCreditScrub();
    }

    // ── Credit Scrub Builder (JS mirror of Python build_credit_scrub) ─────────
    function previousMonthName() {
      const names = ["January","February","March","April","May","June","July","August","September","October","November","December"];
      return names[(new Date().getMonth() + 11) % 12];
    }

    function formatEin(value) {
      const raw = String(value || "").trim();
      const d   = raw.replace(/\D/g,"");
      if (!raw || !d)          return "Tax Id is missing";
      if (d.length < 9)        return "Tax Id is incorrect";
      const c = d.slice(0,9);
      return `${c.slice(0,2)}-${c.slice(2)}`;
    }

    function normalizeMonth(value) {
      const a = {jan:"January",january:"January",feb:"February",february:"February",febuary:"February",mar:"March",march:"March",apr:"April",april:"April",may:"May",jun:"June",june:"June",jul:"July",july:"July",aug:"August",august:"August",sep:"September",sept:"September",september:"September",oct:"October",october:"October",nov:"November",november:"November",dec:"December",december:"December"};
      return a[String(value||"").toLowerCase().replace(/[^a-z]/g,"")] || "";
    }

    function parsePricing() {
      const text = pricingScrub.value.replace(/\r/g,"\n");
      const lines = text.split("\n").map(clean).filter(Boolean);
      let tier = lines[0] || "";
      tier = tier.split(/\bDeposits?(?:\s*\([^)]*\))?\s*:/i)[0].trim();
      if (/^(Deposits|Industry|TIB|State|Datamerch|Company\s+Website|Proceed)\b/i.test(tier)) tier = "";
      const read = (re) => (text.match(re)||["",""])[1].trim();
      const deposits = text.match(/\bDeposits?(?:\s*\(([^)]*)\))?\s*:\s*(.*?)(?:,\s*Industry\s*:|\n|\r|$)/i);
      const website  = read(/Company\s+Website\s*:\s*([^\n\r]*)/i);
      return {
        tier,
        deposits: deposits ? clean(deposits[2]) : "",
        revenueMonth: normalizeMonth(deposits ? deposits[1] : "") || previousMonthName(),
        industry: read(/Industry\s*:\s*([^,\n\r]*)/i),
        tib:      read(/\bTIB\s*:\s*([^,\n\r]*)/i),
        state:    read(/\bState\s*:\s*([^,\n\r]*)/i),
        website:  website && !/^not\s+found$/i.test(website) ? website : "Not Found"
      };
    }

    function googleItem(name, stateValue) {
      name = clean(name);
      if (!name) return "";
      const q = clean(`${name} ${normalizeStateCode(stateValue)}`);
      return { text: `${q} Google Search`, href: `https://www.google.com/search?q=${encodeURIComponent(q)}` };
    }

    function ownerFieldLines(ownerCount, label) {
      if (ownerCount <= 1) return [label];
      const text = String(label).replace(/\s*[:#]\s*$/,"");
      const mark = String(label).trim().endsWith("#") ? "#" : ":";
      return Array.from({length: ownerCount}, (_,i) => `${text} Owner ${i+1} ${mark}`);
    }

    function creditScrubData() {
      const pricing     = parsePricing();
      const scrubState  = state.values.businessState || pricing.state || "";
      const ownerKeys   = ["ownerName","owner2Name","owner3Name","owner4Name"];
      const ownerCount  = Math.max(1, ownerKeys.filter(k => state.values[k]).length);
      const bizSearch   = googleItem(state.values.businessName || state.values.businessDbaName, scrubState);
      const peopleSearch = ownerKeys.map(k => googleItem(state.values[k], scrubState)).filter(Boolean);
      return {
        pricing, bizSearch, peopleSearch, ownerCount,
        infoLines: [
          `Company Website : ${pricing.website || "Not Found"}`, "",
          ...ownerFieldLines(ownerCount, "FICO :"),
          `${pricing.revenueMonth} Revenue : ${pricing.deposits}`,
          `Industry : ${pricing.industry}`, `TIB : ${pricing.tib}`,
          `State : ${normalizeStateCode(scrubState)}`, "Datamerch : No Match",
          `Received : ${receivedCheck.checked ? "Y" : "N"}`,
          `EIN : ${formatEin(state.values.einNumber)}`,
          ...ownerFieldLines(ownerCount, "Customer Id #")
        ]
      };
    }

    function buildCreditScrubPlain() {
      const d = creditScrubData();
      const lines = [];
      if (d.pricing.tier) lines.push(d.pricing.tier, "");
      if (d.bizSearch) lines.push(d.bizSearch.text);
      if (d.bizSearch && d.peopleSearch.length) lines.push("");
      d.peopleSearch.forEach((item, i) => { lines.push(item.text); if (i < d.peopleSearch.length-1) lines.push(""); });
      if (d.bizSearch || d.peopleSearch.length) lines.push("");
      lines.push(...d.infoLines);
      return lines.join("\n").replace(/\n{3,}/g,"\n\n").trimEnd();
    }

    function buildCreditScrubHtml() {
      const d = creditScrubData();
      const parts = [];
      const addLine = (l="") => parts.push(l ? `<div>${escapeHtml(l)}</div>` : "<br>");
      const addLink = (item) => parts.push(`<div><a href="${escapeHtml(item.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.text)}</a></div>`);
      if (d.pricing.tier) { addLine(d.pricing.tier); addLine(""); }
      if (d.bizSearch) addLink(d.bizSearch);
      if (d.bizSearch && d.peopleSearch.length) addLine("");
      d.peopleSearch.forEach((item,i) => { addLink(item); if (i < d.peopleSearch.length-1) addLine(""); });
      if (d.bizSearch || d.peopleSearch.length) addLine("");
      d.infoLines.forEach(line => {
        if (/^Company Website\s*:/i.test(line) && !/Not Found/i.test(line)) {
          const site = line.split(":").slice(1).join(":").trim();
          const href = /^https?:\/\//i.test(site) ? site : `https://${site}`;
          parts.push(`<div>Company Website : <a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(site)}</a></div>`);
        } else { addLine(line); }
      });
      return parts.join("");
    }

    function updateCreditScrub() {
      creditScrub.dataset.plain = buildCreditScrubPlain();
      creditScrub.innerHTML     = buildCreditScrubHtml();
    }

    function resetReceivedAndUpdate() { receivedCheck.checked = false; updateCreditScrub(); }

    // ── Clipboard Helpers ─────────────────────────────────────────────────────
    function wrapClipboardHtml(html) {
      return `<div style="font-family: sans-serif; font-size: 12px; line-height: 1.45;">${html}</div>`;
    }

    async function copyPlainText(text) {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text); return;
      }
      const ta = document.createElement("textarea");
      ta.value = text; ta.setAttribute("readonly",""); ta.style.position = "fixed"; ta.style.left = "-9999px";
      document.body.appendChild(ta); ta.select(); document.execCommand("copy"); document.body.removeChild(ta);
    }

    async function copyFieldValue(key, label, input, button) {
      const value = input.value || state.values[key] || "";
      await copyPlainText(value);
      setStatus(`${label} copied`);
      const orig = button.textContent; button.textContent = "Copied";
      window.setTimeout(() => { button.textContent = orig; }, 900);
    }

    async function copyCreditScrub() {
      const plain = buildCreditScrubPlain();
      const html  = wrapClipboardHtml(buildCreditScrubHtml());
      if (navigator.clipboard && window.ClipboardItem && window.isSecureContext) {
        await navigator.clipboard.write([new ClipboardItem({
          "text/html":  new Blob([html],  {type:"text/html"}),
          "text/plain": new Blob([plain], {type:"text/plain"}),
        })]);
        return;
      }
      const range = document.createRange();
      const sel   = window.getSelection();
      range.selectNodeContents(creditScrub);
      sel.removeAllRanges(); sel.addRange(range);
      document.execCommand("copy"); sel.removeAllRanges();
    }

    // ── Button State ──────────────────────────────────────────────────────────
    function updateButtons() {
      const found = Object.entries(state.values).some(([k,v]) => v && !["businessPhone","businessEmail"].includes(k));
      extractBtn.disabled      = !state.file;
      finalFileName.textContent = state.file ? state.file.name : "No file selected";
      finalSummary.textContent  = found
        ? `${fieldGroups.length} requirement sections filtered from extracted data.`
        : "No filtered data yet.";
    }

    function setFile(file) {
      state.file = file;
      setRawText(""); resetValues(); receivedCheck.checked = false;
      fileName.textContent = file ? file.name : "Drag-drop & select your file";
      setStatus(file ? "File ready" : "Ready");
      setScrubOnly(false); setView("extract"); renderFields(); updateButtons();
    }

    // ── API Calls ─────────────────────────────────────────────────────────────
    async function extractFile() {
      if (!state.file) return;
      setStatus("Processing..."); setProgress(true, 12, "Processing..."); extractBtn.disabled = true;
      const form = new FormData();
      form.append("file",   state.file);
      form.append("engine", $("engine").value);
      try {
        const res  = await fetch("/api/extract", { method:"POST", body:form });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Extraction failed");
        if (data.directExtract) {
          // PDF → Claude direct extraction path
          applyAiResponse(data); renderFields(); setView("final"); setStatus("Done");
          await downloadExcel();
        } else {
          // Image/OCR path — show raw text for review
          setRawText(data.rawText || "");
          resetValues(); Object.assign(state.values, fixedBusinessContact);
          renderFields(); setStatus("Done");
        }
      } catch (err) {
        setStatus("Extraction failed"); alert(err.message || err);
      } finally {
        setProgress(false); updateButtons();
      }
    }

    async function filterRequiredData(scrubOnly = false) {
      setStatus("Processing..."); setProgress(true, 55, "Processing...");
      try {
        const res  = await fetch("/api/filter", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({ rawText: state.rawText })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Processing failed");
        applyAiResponse(data);
        setScrubOnly(scrubOnly);
        if (scrubOnly) { updateButtons(); updateCreditScrub(); } else { renderFields(); }
        setView("final"); setStatus("Done");
        if (!scrubOnly) { await downloadExcel(); }
      } catch (err) {
        setStatus("Processing failed"); alert(err.message || err);
      } finally {
        setProgress(false); updateButtons();
      }
    }

    // ── Status Modal (Proceed / Declined) ─────────────────────────────────────
    function showStatusModal() {
      return new Promise(resolve => {
        const wrap        = $("statusModalWrap");
        const confirmWrap = $("confirmModalWrap");
        const openFirst   = () => { confirmWrap.classList.remove("active"); wrap.classList.add("active"); };
        const openConfirm = () => { wrap.classList.remove("active"); confirmWrap.classList.add("active"); };
        function cleanup() {
          wrap.classList.remove("active"); confirmWrap.classList.remove("active");
          $("proceedBtn").removeEventListener("click", onProceed);
          $("declinedBtn").removeEventListener("click", onDeclined);
          $("confirmYesBtn").removeEventListener("click", onConfirmYes);
          $("confirmBackBtn").removeEventListener("click", onConfirmBack);
        }
        const onProceed    = () => openConfirm();
        const onDeclined   = () => { cleanup(); resolve("Declined & Received on"); };
        const onConfirmYes = () => { cleanup(); resolve("Proceed"); };
        const onConfirmBack = () => openFirst();
        $("proceedBtn").addEventListener("click", onProceed);
        $("declinedBtn").addEventListener("click", onDeclined);
        $("confirmYesBtn").addEventListener("click", onConfirmYes);
        $("confirmBackBtn").addEventListener("click", onConfirmBack);
        openFirst();
      });
    }

    async function downloadExcel() {
      const status = await showStatusModal();
      try {
        const res  = await fetch("/api/export", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({ values: state.values, status })
        });
        const data = await res.json().catch(() => ({}));
        setStatus(!res.ok || !data.ok
          ? (data.message || data.error || "Google Sheet save failed")
          : `${status} - ${data.message || "Saved to Google Sheet"}`
        );
      } catch { setStatus("Google Sheet save failed"); }
    }

    async function clearSheets() {
      if (!window.confirm("Are you sure? Sheet 1 data will be cleared from all 4 Google Sheets, except column A.")) return;
      const password = window.prompt("Enter password to clear sheets");
      if (password === null) return;
      if (password !== "2580") { setStatus("Wrong password"); alert("Wrong password"); return; }
      setStatus("Clearing Google Sheets..."); sheetClearBtn.disabled = true;
      try {
        const res  = await fetch("/api/sheet-clear", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({password}) });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) throw new Error(data.error || data.message || "Sheet clear failed");
        setStatus(data.message || "Google Sheets cleared"); alert(data.message || "Google Sheets cleared");
      } catch (err) { setStatus("Sheet clear failed"); alert(err.message || err); }
      finally { sheetClearBtn.disabled = false; }
    }

    function clearAll() {
      state.file = null; resetValues(); setRawText(""); fileInput.value = "";
      pricingScrub.value = ""; receivedCheck.checked = false;
      setProgress(false); setScrubOnly(false); setView("extract"); setFile(null); renderFields();
    }

    // ── Event Listeners ───────────────────────────────────────────────────────
    fileInput.addEventListener("change",  ev => setFile(ev.target.files[0] || null));
    extractBtn.addEventListener("click",  extractFile);
    scrubOnlyBtn.addEventListener("click",() => { window.location.href = "/scrub-only"; });
    sheetClearBtn.addEventListener("click", clearSheets);
    clearBtn.addEventListener("click",    clearAll);
    clearFinalBtn.addEventListener("click", clearAll);
    backBtn.addEventListener("click",     () => { setView("extract"); setStatus(state.rawText ? "Done" : "Ready"); updateButtons(); });
    pricingScrub.addEventListener("input", resetReceivedAndUpdate);
    receivedCheck.addEventListener("change", updateCreditScrub);

    // Custom copy handler for the credit scrub — copies as rich HTML + plain text
    creditScrub.addEventListener("copy", ev => {
      const sel     = window.getSelection();
      const selHtml = sel && sel.rangeCount
        ? (() => { const r=sel.getRangeAt(0); if(!creditScrub.contains(r.commonAncestorContainer)) return ""; const w=document.createElement("div"); w.appendChild(r.cloneContents()); return wrapClipboardHtml(w.innerHTML); })()
        : "";
      const html  = selHtml || wrapClipboardHtml(buildCreditScrubHtml());
      const plain = sel && String(sel).trim() ? String(sel) : buildCreditScrubPlain();
      ev.preventDefault();
      ev.clipboardData.setData("text/html",  html);
      ev.clipboardData.setData("text/plain", plain);
    });

    $("copyCreditBtn").addEventListener("click", async () => {
      await copyCreditScrub(); setStatus("Credit scrub copied");
    });

    // Drag-and-drop file upload
    ["dragenter","dragover"].forEach(n => $("dropzone").addEventListener(n, ev => { ev.preventDefault(); $("dropzone").classList.add("dragging"); }));
    ["dragleave","drop"].forEach(n =>     $("dropzone").addEventListener(n, ev => { ev.preventDefault(); $("dropzone").classList.remove("dragging"); }));
    $("dropzone").addEventListener("drop", ev => { const f = ev.dataTransfer.files[0]; if (f) setFile(f); });

    // Initialize
    renderFields();
  </script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — Page Builders
# ══════════════════════════════════════════════════════════════════════════════

def html_page() -> bytes:
    """Build the main application HTML page with injected field definitions."""
    groups       = [{"name": name, "fields": fields} for name, fields in FIELD_GROUPS]
    mode_options = "\n".join(f"<option>{mode}</option>" for mode in OCR_MODES)
    page = (
        HTML
        .replace("__FIELD_GROUPS__",     json.dumps(groups))
        .replace("__FIXED_CONTACT__",    json.dumps(FIXED_BUSINESS_CONTACT))
        .replace("__OCR_MODE_OPTIONS__", mode_options)
    )
    return page.encode("utf-8")


def scrub_only_page() -> bytes:
    """Read and return the scrub-only HTML page bytes."""
    if not SCRUB_ONLY_HTML_PATH.exists():
        raise RuntimeError(f"Scrub-only page not found: {SCRUB_ONLY_HTML_PATH}")
    return SCRUB_ONLY_HTML_PATH.read_bytes()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — Multipart Form Parsing
# ══════════════════════════════════════════════════════════════════════════════

def parse_multipart(content_type: str, body: bytes) -> dict[str, tuple[str, bytes] | str]:
    """
    Parse a multipart/form-data request body into a dict.
    File uploads return (filename, bytes), text fields return str.
    Uses Python's built-in email.parser for compatibility (no third-party deps).
    """
    headers  = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message  = BytesParser(policy=default).parsebytes(headers + body)
    fields:  dict[str, tuple[str, bytes] | str] = {}

    if not message.is_multipart():
        return fields

    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if "form-data" not in disposition:
            continue
        name     = part.get_param("name",     header="content-disposition")
        filename = part.get_param("filename", header="content-disposition")
        payload  = part.get_payload(decode=True) or b""
        if filename:
            fields[str(name)] = (str(filename), payload)
        elif name:
            fields[str(name)] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")

    return fields


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 12 — Excel Export
# ══════════════════════════════════════════════════════════════════════════════

def export_rows(values: dict[str, str]) -> tuple[list[str], list[str]]:
    """Build Excel header and data row from frontend values dict."""
    values  = {**values, **FIXED_BUSINESS_CONTACT}
    headers = [label for _, label in EXCEL_COLUMNS]
    data    = []
    for key, _ in EXCEL_COLUMNS:
        val = values.get(key, "")
        if key == "einNumber":
            val = format_ein(val)   # EIN stored as digits only
        data.append(val)
    return headers, data


def build_xlsx(values: dict[str, str]) -> bytes:
    """
    Append a new row to the local Excel export file (credit_export.xlsx).
    Creates the file with headers if it doesn't exist.
    Uses openpyxl for formatting (bold headers, thin borders, column widths).
    Thread-safe via _EXCEL_LOCK.
    """
    openpyxl        = importlib.import_module("openpyxl")
    openpyxl_styles = importlib.import_module("openpyxl.styles")
    openpyxl_utils  = importlib.import_module("openpyxl.utils")

    headers, data_row = export_rows(values)
    thin        = openpyxl_styles.Side(style="thin", color="D9DEE7")
    header_fill = openpyxl_styles.PatternFill(start_color="E7F4F2", end_color="E7F4F2", fill_type="solid")

    def _style_row(sheet, row_idx, is_header=False):
        for col_idx in range(1, len(headers) + 1):
            cell = sheet.cell(row_idx, col_idx)
            cell.alignment = openpyxl_styles.Alignment(horizontal="left", vertical="center", wrap_text=True)
            cell.border    = openpyxl_styles.Border(top=thin, bottom=thin, left=thin, right=thin)
            if is_header:
                cell.font = openpyxl_styles.Font(bold=True)
                cell.fill = header_fill

    with _EXCEL_LOCK:
        if EXCEL_EXPORT_PATH.exists():
            workbook = openpyxl.load_workbook(EXCEL_EXPORT_PATH)
            sheet    = workbook.active
            next_row = sheet.max_row + 1
        else:
            workbook = openpyxl.Workbook()
            sheet    = workbook.active
            sheet.title = "Extracted Details"
            for col_idx, header in enumerate(headers, start=1):
                sheet.cell(1, col_idx).value = header
            _style_row(sheet, 1, is_header=True)
            for col_idx in range(1, len(headers) + 1):
                sheet.column_dimensions[openpyxl_utils.get_column_letter(col_idx)].width = 24
            next_row = 2

        for col_idx, val in enumerate(data_row, start=1):
            sheet.cell(next_row, col_idx).value = val
        _style_row(sheet, next_row)

        workbook.save(EXCEL_EXPORT_PATH)
        return EXCEL_EXPORT_PATH.read_bytes()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 13 — HTTP Request Handler
# ══════════════════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    """
    HTTP request handler for the credit extraction web app.
    Routes GET requests to HTML pages and POST requests to API handlers.
    All errors are returned as JSON with an "error" key.
    """
    server_version = "CreditOCRHTTP/1.0"

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}")

    def send_bytes(
        self,
        content:      bytes,
        status:       int              = 200,
        content_type: str              = "text/plain; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type",   content_type)
        self.send_header("Content-Length", str(len(content)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, data: dict, status: int = 200) -> None:
        self.send_bytes(json.dumps(data).encode("utf-8"), status, "application/json; charset=utf-8")

    # ── GET Routes ────────────────────────────────────────────────────────────
    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self.send_bytes(html_page(), content_type="text/html; charset=utf-8")
        elif path in {"/scrub-only", "/scrub-only.html"}:
            self.send_bytes(scrub_only_page(), content_type="text/html; charset=utf-8")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    # ── POST Routes ───────────────────────────────────────────────────────────
    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if   path == "/api/extract":     self.handle_extract()
            elif path == "/api/filter":      self.handle_filter()
            elif path == "/api/export":      self.handle_export()
            elif path == "/api/sheet-clear": self.handle_sheet_clear()
            else:                            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    # ── /api/extract ─────────────────────────────────────────────────────────
    def handle_extract(self) -> None:
        """
        Handle file upload.
        - PDF → send directly to Claude AI (no OCR)
        - Image → run OCR pipeline, return raw text for user review
        """
        content_type = self.headers.get("Content-Type", "")
        length       = int(self.headers.get("Content-Length", "0"))
        fields       = parse_multipart(content_type, self.rfile.read(length))
        upload       = fields.get("file")

        if not isinstance(upload, tuple):
            self.send_json({"error": "File upload missing."}, 400)
            return

        filename, payload = upload
        suffix            = Path(filename).suffix.lower()
        progress_messages: list[str] = []

        if suffix == ".pdf":
            # PDF path: Claude reads the PDF directly (better for handwriting/scanned forms)
            ai_data = extract_ai_json_from_pdf(payload, progress_messages.append)
            values  = ai_json_to_frontend_values(ai_data)
            scrub   = build_credit_scrub(values, "")
            self.send_json({
                "business_info":      ai_data.get("business_info", {}),
                "owners":             ai_data.get("owners", []),
                "lander_name":        values.get("lander_name", ""),
                "representative_name": values.get("representative_name", ""),
                "iso_email":          values.get("iso_email", ""),
                "creditScrub":        scrub,
                "status":             "Done",
                "progress":           progress_messages,
                "directExtract":      True,   # Tells frontend to skip raw text view
            })
        else:
            # Image path: OCR first, then show raw text to user
            engine      = fields.get("engine", "Image")
            upload_mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            file_suffix = Path(filename).suffix or mimetypes.guess_extension(upload_mime) or ".bin"
            fd, temp_name = tempfile.mkstemp(prefix="ocr_upload_", suffix=file_suffix)
            os.close(fd)
            temp_path = Path(temp_name)
            temp_path.write_bytes(payload)
            pipeline = OCRPipeline(progress_messages.append)
            try:
                raw_text     = pipeline.run(temp_path, str(engine))
                display_text = clean_raw_ocr_text_for_display(raw_text)
                self.send_json({
                    "values":      FIXED_BUSINESS_CONTACT,
                    "rawText":     display_text,
                    "creditScrub": build_credit_scrub(FIXED_BUSINESS_CONTACT, ""),
                    "status":      "Done",
                    "progress":    progress_messages,
                })
            finally:
                pipeline.cleanup()
                temp_path.unlink(missing_ok=True)

    # ── /api/filter ──────────────────────────────────────────────────────────
    def handle_filter(self) -> None:
        """
        Run AI extraction on raw OCR text submitted from the frontend.
        Returns structured JSON + lender name (from all_lander_name.txt lookup).
        """
        length   = int(self.headers.get("Content-Length", "0"))
        payload  = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        raw_text = str(payload.get("rawText") or "")

        if not clean_raw_ocr_text_for_display(raw_text):
            self.send_json({"error": "Raw OCR text missing. First extract PDF data."}, 400)
            return

        progress_messages: list[str] = []
        ai_data = extract_ai_json(raw_text, progress_messages.append)
        values  = ai_json_to_frontend_values(ai_data)

        # Lander name: prefer context search over AI output (more reliable)
        values["lander_name"] = find_lander_name(raw_text) or values.get("lander_name", "")
        scrub = build_credit_scrub(values, "")

        self.send_json({
            "business_info":      ai_data.get("business_info", {}),
            "owners":             ai_data.get("owners", []),
            "lander_name":        values["lander_name"],
            "representative_name": values.get("representative_name", ""),
            "iso_email":          values.get("iso_email", ""),
            "creditScrub":        scrub,
            "status":             "Done",
            "progress":           progress_messages,
        })

    # ── /api/export ──────────────────────────────────────────────────────────
    def handle_export(self) -> None:
        """
        Save extracted values to Google Sheets.
        Returns sheet URL and submission number on success.
        """
        length  = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        values  = {**(payload.get("values") or {}), **FIXED_BUSINESS_CONTACT}
        values["status"] = str(payload.get("status") or "")

        sheet_result = try_append_credit_google_row(values)
        skipped      = bool(sheet_result.get("skipped"))

        self.send_json({
            "ok":      bool(sheet_result.get("ok", False) or skipped),
            "skipped": skipped,
            "message": str(sheet_result.get("message") or ""),
            "url":     sheet_result.get("url", ""),
            "row":     sheet_result.get("row", 0),
        })

    # ── /api/sheet-clear ─────────────────────────────────────────────────────
    def handle_sheet_clear(self) -> None:
        """
        Clear all submission data from all configured Google Sheets.
        Password-protected: requires "2580" from the client.
        """
        length  = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")

        if str(payload.get("password") or "") != "2580":
            self.send_json({"ok": False, "error": "Wrong password"}, 403)
            return

        result = clear_credit_sheet_data()
        self.send_json(result)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 14 — Server Entry Point
# ══════════════════════════════════════════════════════════════════════════════

def run_server(open_browser: bool = True) -> None:
    """
    Start the HTTP server and optionally open the browser.
    The browser opens after a short delay to let the server bind its port first.
    """
    url    = f"http://{HOST}:{PORT}/"
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Running on {url}", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    # Set OCR_WEB_OPEN=1 in .env to auto-open the browser on startup
    run_server(os.getenv("OCR_WEB_OPEN", "0") == "1")
