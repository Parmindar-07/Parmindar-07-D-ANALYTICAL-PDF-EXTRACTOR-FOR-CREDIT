"""
================================================================================
  google_sheet_sync.py — Google Sheets Integration
================================================================================

  PURPOSE:
    Syncs extracted credit application data to one or more Google Sheets.
    Data is written vertically (labels in column A, values in subsequent columns)
    rather than the standard horizontal row-per-record format.

  SHEET LAYOUT (vertical format):
    Column A = Field labels (frozen, written once)
    Column B = First submission's values
    Column C = Second submission's values
    ... and so on

  MULTI-SHEET SUPPORT:
    Configure up to 9 Google Sheets via environment variables.
    The app rotates between sheets using a counter file (sheet_counter.txt).

  OWNER SPLITTING:
    If a submission has 3-4 owners, two rows are written:
      Row 1: Owners 1 & 2
      Row 2: Owners 3 & 4 (mapped to positions 1 & 2 for the second row)

  ENVIRONMENT VARIABLES:
    CREDIT_GOOGLE_SHEET_ENABLED   — "true"/"false" (default: true)
    CREDIT_GOOGLE_CREDENTIALS_PATH — path to service account JSON file
    GOOGLE_SHEET_ID_1 through _9  — Google Spreadsheet IDs or full URLs
    GOOGLE_SHEET_1_ENABLED ...    — per-sheet enable/disable flags
    CREDIT_GOOGLE_WORKSHEET_NAME  — worksheet tab name (default: first sheet)

  CREDENTIALS:
    Use a Google Service Account JSON file.
    The service account must have Editor access to each configured spreadsheet.
    Download from: Google Cloud Console → IAM → Service Accounts

  INSTALLATION:
    pip install gspread
================================================================================
"""

from __future__ import annotations

import importlib
import os
import re
import threading
from pathlib import Path


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Constants & Configuration
# ══════════════════════════════════════════════════════════════════════════════

APP_DIR = Path(__file__).resolve().parent

# Default sheet ID (used when no GOOGLE_SHEET_ID_* env vars are set)
DEFAULT_CREDIT_SHEET_ID = "1B-BnzuYH2tJW5X8Rrs2OgMUs5CuAhFDOCMy2rbEKPfQ"

# Sheet IDs for the "Sheet Clear" operation (clears all 4 sheets at once)
SHEET_CLEAR_IDS = [
    "1xRYetrJ-Vqh7h0rJRMiQs-zlWrfhjJY0DIOW5KMnMAM",
    "1bmhq2jzW9Z7bg4cgSlGW4AlxPc2fL4BIIFxJx7ZpRqM",
    "14HTxWnVSBoWVuqxz8EVdqSu1MUCvwylvp0MeuXzFbQE",
    "1Q3kNRO5tJ8lYN6gK2K9ifZOL6n-k09wZlpdc9z2ozw4",
]

# Counter file tracks which sheet to write to next (round-robin rotation)
_COUNTER_FILE = APP_DIR / "sheet_counter.txt"

# Fixed contact info always added to submissions
DEFAULT_FIXED_BUSINESS_CONTACT = {
    "businessPhone": "6468459754",
    "businessEmail": "contracts@tvtcapital.com",
}

# Thread lock — prevents concurrent sheet writes from corrupting column placement
_GSHEETS_LOCK = threading.Lock()

# Environment variable keys that should always be re-read (not cached from system env)
CREDIT_ENV_OVERRIDE_KEYS = {
    "CREDIT_GOOGLE_SHEET_ENABLED",
    "CREDIT_GOOGLE_SHEET_ID",
    "CREDIT_GOOGLE_SHEET_URL",
    "CREDIT_GOOGLE_WORKSHEET_NAME",
    "CREDIT_GOOGLE_CREDENTIALS_PATH",
    "GOOGLE_SHEET_ENABLED",
    "GOOGLE_SHEETS_ENABLED",
    "GOOGLE_SHEET_ID",
    "GOOGLE_SHEET_URL",
    "GOOGLE_WORKSHEET_NAME",
    "GOOGLE_CREDENTIALS_PATH",
}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Column Definitions
# ══════════════════════════════════════════════════════════════════════════════

# Full state name lookup (state code → full name)
STATE_FULL_NAMES = {
    "AL": "Alabama",       "AK": "Alaska",        "AZ": "Arizona",
    "AR": "Arkansas",      "CA": "California",     "CO": "Colorado",
    "CT": "Connecticut",   "DE": "Delaware",       "FL": "Florida",
    "GA": "Georgia",       "HI": "Hawaii",         "IA": "Iowa",
    "ID": "Idaho",         "IL": "Illinois",       "IN": "Indiana",
    "KS": "Kansas",        "KY": "Kentucky",       "LA": "Louisiana",
    "MA": "Massachusetts", "MD": "Maryland",       "ME": "Maine",
    "MI": "Michigan",      "MN": "Minnesota",      "MO": "Missouri",
    "MS": "Mississippi",   "MT": "Montana",        "NC": "North Carolina",
    "ND": "North Dakota",  "NE": "Nebraska",       "NH": "New Hampshire",
    "NJ": "New Jersey",    "NM": "New Mexico",     "NV": "Nevada",
    "NY": "New York",      "OH": "Ohio",           "OK": "Oklahoma",
    "OR": "Oregon",        "PA": "Pennsylvania",   "RI": "Rhode Island",
    "SC": "South Carolina","SD": "South Dakota",   "TN": "Tennessee",
    "TX": "Texas",         "UT": "Utah",           "VA": "Virginia",
    "VT": "Vermont",       "WA": "Washington",     "WI": "Wisconsin",
    "WV": "West Virginia", "WY": "Wyoming",        "DC": "District of Columbia",
}

# Sheet column definitions: (frontend_key, column_label_in_sheet)
# Order matches how labels appear in column A of the Google Sheet
SHEET_COLUMNS = [
    ("businessName",      "Business Legal Name"),
    ("businessDbaName",   "Business DBA Name"),
    ("businessAddress",   "Business Address"),
    ("businessState",     "Business State"),
    ("businessCity",      "Business City"),
    ("businessStateFull", "Business State Full Name"),
    ("businessZip",       "Business Zip"),
    ("businessPhone",     "Business Phone Number"),
    ("businessEmail",     "Business Gmail"),
    ("einNumber",         "Tax ID (EIN #)"),
    ("lander_name",           "Lander Name"),
    ("representative_name",   "Representative Name"),
    ("iso_email",             "ISO Email"),
    ("ownerName",         "Owner 1 Name"),
    ("ownerDob",          "Owner 1 DOB"),
    ("ownerAddress",      "Owner 1 Home Address"),
    ("ownerCity",         "Owner 1 City"),
    ("ownerState",        "Owner 1 State"),
    ("ownerStateFull",    "Owner 1 State Full Name"),
    ("ownerZip",          "Owner 1 Zip"),
    ("ssn",               "Owner 1 SSN#"),
    ("owner2Name",        "Owner 2 Name"),
    ("owner2Dob",         "Owner 2 DOB"),
    ("owner2Address",     "Owner 2 Home Address"),
    ("owner2City",        "Owner 2 City"),
    ("owner2State",       "Owner 2 State"),
    ("owner2StateFull",   "Owner 2 State Full Name"),
    ("owner2Zip",         "Owner 2 Zip"),
    ("owner2Ssn",         "Owner 2 SSN#"),
    ("owner3Name",        "Owner 3 Name"),
    ("owner3Dob",         "Owner 3 DOB"),
    ("owner3Address",     "Owner 3 Home Address"),
    ("owner3City",        "Owner 3 City"),
    ("owner3State",       "Owner 3 State"),
    ("owner3StateFull",   "Owner 3 State Full Name"),
    ("owner3Zip",         "Owner 3 Zip"),
    ("owner3Ssn",         "Owner 3 SSN#"),
    ("owner4Name",        "Owner 4 Name"),
    ("owner4Dob",         "Owner 4 DOB"),
    ("owner4Address",     "Owner 4 Home Address"),
    ("owner4City",        "Owner 4 City"),
    ("owner4State",       "Owner 4 State"),
    ("owner4StateFull",   "Owner 4 State Full Name"),
    ("owner4Zip",         "Owner 4 Zip"),
    ("owner4Ssn",         "Owner 4 SSN#"),
    ("_custId",           "Customer Id #"),
    ("_status47",         "Status"),
    ("status",            "P/D"),   # Proceed / Declined status from modal
]

# Owner key groups — used when splitting 3-4 owner submissions into 2 rows
OWNER_KEY_GROUPS = [
    ("ownerName",  "ownerDob",  "ownerAddress",  "ownerCity",  "ownerState",  "ownerStateFull",  "ownerZip",  "ssn"),
    ("owner2Name", "owner2Dob", "owner2Address", "owner2City", "owner2State", "owner2StateFull", "owner2Zip", "owner2Ssn"),
    ("owner3Name", "owner3Dob", "owner3Address", "owner3City", "owner3State", "owner3StateFull", "owner3Zip", "owner3Ssn"),
    ("owner4Name", "owner4Dob", "owner4Address", "owner4City", "owner4State", "owner4StateFull", "owner4Zip", "owner4Ssn"),
]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Environment Loading
# ══════════════════════════════════════════════════════════════════════════════

def _is_credit_env_override_key(key: str) -> bool:
    """Return True if this env key should always be re-read from .env."""
    return (
        key in CREDIT_ENV_OVERRIDE_KEYS
        or re.fullmatch(r"GOOGLE_SHEET_(?:ID_\d+|\d+_ENABLED)", key) is not None
    )


def _load_env_file(path: Path) -> None:
    """Parse a .env file and set variables in os.environ."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key   = key.strip()
        value = value.strip().strip('"').strip("'")
        # Override keys are always re-read; others respect system env
        if key and (_is_credit_env_override_key(key) or key not in os.environ):
            os.environ[key] = value


def load_credit_env() -> None:
    """
    Load .env from the current working directory and app directory.
    Called at startup and before each Google Sheet operation.
    """
    _load_env_file(Path.cwd() / ".env")
    _load_env_file(APP_DIR / ".env")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Helper Utilities
# ══════════════════════════════════════════════════════════════════════════════

def _clean(value: object) -> str:
    """Strip leading/trailing whitespace and punctuation."""
    text = re.sub(r"\s+", " ", str(value or ""))
    return re.sub(r"^[\s:.\-]+|[\s:.\-]+$", "", text).strip()


def _env_enabled(name: str, default: str = "1") -> bool:
    """Return True if an env variable represents a truthy value."""
    return _clean(os.getenv(name, default)).lower() in {"1", "true", "yes", "on"}


def _env_disabled_value(value: str) -> bool:
    """Return True if a string value represents a falsy/disabled state."""
    return _clean(value).lower() in {"0", "false", "no", "off"}


def _sheet_id_from_value(value: str) -> str:
    """
    Extract a Google Spreadsheet ID from either a bare ID or a full URL.
    Example URL: https://docs.google.com/spreadsheets/d/SHEET_ID/edit
    """
    text  = _clean(value)
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", text)
    return match.group(1) if match else text


def _column_letter(index: int) -> str:
    """Convert a 1-based column index to a spreadsheet column letter (1→A, 27→AA)."""
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _state_full_name(value: str) -> str:
    """Convert a 2-letter state code to its full name (e.g., "NY" → "New York")."""
    state = re.sub(r"[^A-Z0-9]", "", _clean(value).upper())
    if state in {"FI", "F1"}:   # Common OCR misread of "FL"
        state = "FL"
    return STATE_FULL_NAMES.get(state, "")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Sheet ID Selection (Round-Robin)
# ══════════════════════════════════════════════════════════════════════════════

def _all_sheet_ids() -> list[str]:
    """
    Collect all configured Google Sheet IDs from env variables.
    Reads GOOGLE_SHEET_ID_1 through GOOGLE_SHEET_ID_9.
    Falls back to CREDIT_GOOGLE_SHEET_ID or DEFAULT_CREDIT_SHEET_ID if none set.
    """
    ids: list[str] = []
    for i in range(1, 10):
        enabled_raw = os.getenv(f"GOOGLE_SHEET_{i}_ENABLED", "true")
        if _env_disabled_value(enabled_raw):
            continue
        raw = os.getenv(f"GOOGLE_SHEET_ID_{i}", "")
        if raw:
            sid = _sheet_id_from_value(raw)
            if sid:
                ids.append(sid)
    if not ids:
        fallback = (
            os.getenv("CREDIT_GOOGLE_SHEET_ID")
            or os.getenv("GOOGLE_SHEET_ID")
            or DEFAULT_CREDIT_SHEET_ID
        )
        ids = [_sheet_id_from_value(fallback)]
    return ids


def _has_numbered_sheet_config() -> bool:
    """Return True if any GOOGLE_SHEET_{i}_* env vars are configured."""
    return any(
        os.getenv(f"GOOGLE_SHEET_{i}_ENABLED") is not None
        or os.getenv(f"GOOGLE_SHEET_ID_{i}") is not None
        for i in range(1, 10)
    )


def _numbered_sheet_sync_enabled() -> bool:
    """Return True if at least one numbered sheet is configured and enabled."""
    for i in range(1, 10):
        raw = os.getenv(f"GOOGLE_SHEET_ID_{i}", "")
        if not _clean(raw):
            continue
        if not _env_disabled_value(os.getenv(f"GOOGLE_SHEET_{i}_ENABLED", "true")):
            return True
    return False


def _google_sheet_sync_enabled() -> bool:
    """Return True if Google Sheet sync is globally and per-sheet enabled."""
    if not _env_enabled("CREDIT_GOOGLE_SHEET_ENABLED", os.getenv("GOOGLE_SHEET_ENABLED", "1")):
        return False
    if _has_numbered_sheet_config():
        return _numbered_sheet_sync_enabled()
    return True


def _next_sheet_id() -> str:
    """
    Return the next sheet ID to write to (round-robin rotation).
    Uses sheet_counter.txt to track position across server restarts.
    """
    ids = _all_sheet_ids()
    if len(ids) == 1:
        return ids[0]
    try:
        counter = int(_COUNTER_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        counter = 0
    index = counter % len(ids)
    _COUNTER_FILE.write_text(str(counter + 1), encoding="utf-8")
    return ids[index]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — Credentials Discovery
# ══════════════════════════════════════════════════════════════════════════════

def _credential_candidates(raw_path: str) -> list[Path]:
    """
    Build a prioritized list of paths to check for the Google credentials file.
    Checks configured path first, then several standard locations.
    """
    candidates: list[Path] = []

    def add(path: Path) -> None:
        if path not in candidates:
            candidates.append(path)

    if raw_path:
        path = Path(raw_path).expanduser()
        if path.is_absolute():
            add(path)
        else:
            add(Path.cwd() / path)
            add(APP_DIR / path)
            add(APP_DIR.parent / path)

    # Standard fallback locations
    add(APP_DIR.parent / "SF" / "credentials.json")
    add(Path.cwd()    / "SF" / "credentials.json")
    add(APP_DIR       / "credentials.json")
    add(APP_DIR       / "service_account.json")
    add(Path.cwd()    / "service_account.json")
    return candidates


def _credentials_path() -> Path:
    """
    Find the Google service account credentials file.

    Raises:
        FileNotFoundError: If credentials file is not found in any candidate location.
    """
    raw        = os.getenv("CREDIT_GOOGLE_CREDENTIALS_PATH") or os.getenv("GOOGLE_CREDENTIALS_PATH") or ""
    candidates = _credential_candidates(raw)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    checked = "; ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"Google credentials file not found.\n"
        f"Checked locations:\n  {checked}\n\n"
        f"Set CREDIT_GOOGLE_CREDENTIALS_PATH in .env to the correct path."
    )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — Data Preparation
# ══════════════════════════════════════════════════════════════════════════════

def _prepared_values(values: dict[str, str]) -> dict[str, str]:
    """
    Normalize and enrich a frontend values dict before writing to the sheet.
    - Strips whitespace from all values
    - Fills missing fixed contact fields
    - Computes full state names from state codes
    """
    prepared = {key: _clean(value) for key, value in (values or {}).items()}

    # Ensure fixed contact fields are always present
    for key, value in DEFAULT_FIXED_BUSINESS_CONTACT.items():
        if not prepared.get(key):
            prepared[key] = value

    # Auto-compute state full names from state codes
    for state_key, full_key in {
        "businessState": "businessStateFull",
        "ownerState":    "ownerStateFull",
        "owner2State":   "owner2StateFull",
        "owner3State":   "owner3StateFull",
        "owner4State":   "owner4StateFull",
    }.items():
        if not prepared.get(full_key):
            prepared[full_key] = _state_full_name(prepared.get(state_key, ""))

    return prepared


def _sheet_value(key: str, value: str) -> str:
    """Apply field-specific formatting rules before writing to sheet."""
    if key == "einNumber":
        # Store EIN as digits only (no hyphens) in the sheet
        digits = re.sub(r"\D", "", value)
        return digits[:9] if digits else value
    return value


def _headers() -> list[str]:
    """Return ordered list of column header labels for column A."""
    return [label for _key, label in SHEET_COLUMNS]


def _row_values_from_prepared(prepared: dict[str, str]) -> list[str]:
    """Build a single column of values matching the SHEET_COLUMNS order."""
    return [_sheet_value(key, prepared.get(key, "")) for key, _label in SHEET_COLUMNS]


def _row_values(values: dict[str, str]) -> list[str]:
    """Prepare and return row values for a single submission."""
    return _row_values_from_prepared(_prepared_values(values))


def _owner_has_data(prepared: dict[str, str], owner_index: int) -> bool:
    """Return True if the given owner slot has any non-empty, non-full-name fields."""
    keys = OWNER_KEY_GROUPS[owner_index]
    return any(prepared.get(key, "") for key in keys if not key.endswith("StateFull"))


def _clear_owner(prepared: dict[str, str], owner_index: int) -> None:
    """Clear all fields for an owner slot in a prepared values dict."""
    for key in OWNER_KEY_GROUPS[owner_index]:
        prepared[key] = ""


def _copy_owner(prepared: dict[str, str], source_index: int, target_index: int) -> None:
    """Copy all fields from one owner slot to another."""
    for source_key, target_key in zip(OWNER_KEY_GROUPS[source_index], OWNER_KEY_GROUPS[target_index]):
        prepared[target_key] = prepared.get(source_key, "")


def _row_value_sets(values: dict[str, str]) -> list[list[str]]:
    """
    Generate one or two column value sets for the Google Sheet.

    If there are 3-4 owners, split into two columns:
      Column 1: Business info + Owners 1 & 2
      Column 2: Same business info + Owners 3 & 4 (moved to slots 1 & 2)

    If there are 1-2 owners, return a single column.
    """
    prepared = _prepared_values(values)

    # Check if Owners 3 or 4 have any data
    if not (_owner_has_data(prepared, 2) or _owner_has_data(prepared, 3)):
        return [_row_values_from_prepared(prepared)]

    # First column: Owners 1 & 2 only
    first_pair = dict(prepared)
    _clear_owner(first_pair, 2)
    _clear_owner(first_pair, 3)

    # Second column: Owners 3 & 4 moved to slots 1 & 2
    second_pair = dict(prepared)
    _copy_owner(second_pair, 2, 0)
    _copy_owner(second_pair, 3, 1)
    _clear_owner(second_pair, 2)
    _clear_owner(second_pair, 3)

    return [
        _row_values_from_prepared(first_pair),
        _row_values_from_prepared(second_pair),
    ]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — Worksheet Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _worksheet(spreadsheet):
    """Return the configured worksheet, or the first sheet if no name is set."""
    worksheet_name = _clean(os.getenv("CREDIT_GOOGLE_WORKSHEET_NAME", ""))
    if worksheet_name:
        return spreadsheet.worksheet(worksheet_name)
    return spreadsheet.sheet1


def _ensure_col_a_labels(spreadsheet, worksheet, labels: list[str]) -> None:
    """
    Write field labels to column A if they are not already there.
    Also freezes column A so it stays visible when scrolling horizontally.
    """
    existing = worksheet.col_values(1)
    if existing[:len(labels)] == labels:
        return   # Labels are already correct — skip write

    label_cells = [[label] for label in labels]
    worksheet.update(
        range_name=f"A1:A{len(labels)}",
        values=label_cells,
        value_input_option="RAW",
    )
    # Freeze column A (try the simple method first, then the batch update)
    try:
        worksheet.freeze(cols=1)
    except Exception:
        try:
            spreadsheet.batch_update({
                "requests": [{
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": worksheet.id,
                            "gridProperties": {"frozenColumnCount": 1},
                        },
                        "fields": "gridProperties.frozenColumnCount",
                    }
                }]
            })
        except Exception:
            pass   # Freeze failed — non-critical, data still writes correctly


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — Main Sheet Operations
# ══════════════════════════════════════════════════════════════════════════════

def append_credit_google_row(values: dict[str, str]) -> tuple[str, int, int]:
    """
    Write extracted values to the next available column in the Google Sheet.

    Column A contains field labels (written once on first use).
    Each new submission adds a new column to the right.

    Returns:
        (sheet_url, submission_number, columns_written)

    Raises:
        RuntimeError: If sheet ID is missing.
        FileNotFoundError: If credentials file is not found.
        ImportError: If gspread is not installed.
    """
    load_credit_env()
    sheet_id = _next_sheet_id()
    if not sheet_id:
        raise RuntimeError("Google Sheet ID missing. Set GOOGLE_SHEET_ID_1 in .env")

    credentials_path = _credentials_path()

    try:
        gspread = importlib.import_module("gspread")
    except ImportError as exc:
        raise ImportError("gspread not installed. Run: pip install gspread") from exc

    labels         = _headers()
    col_value_sets = _row_value_sets(values)

    with _GSHEETS_LOCK:
        gc          = gspread.service_account(filename=str(credentials_path))
        spreadsheet = gc.open_by_key(sheet_id)
        worksheet   = _worksheet(spreadsheet)

        # Ensure column A has all field labels
        _ensure_col_a_labels(spreadsheet, worksheet, labels)

        # Find the next empty column (use full width to avoid partial-row gaps)
        all_data     = worksheet.get_all_values()
        current_width = len(all_data[0]) if all_data else 0
        next_col      = max(current_width + 1, 2)   # Column B minimum (column A = labels)

        # Expand sheet dimensions if needed
        required_cols = next_col + len(col_value_sets) - 1
        if required_cols > worksheet.col_count:
            worksheet.resize(cols=required_cols + 20)
        if len(labels) > worksheet.row_count:
            worksheet.resize(rows=len(labels) + 10)

        # Write each column of values
        for offset, col_values in enumerate(col_value_sets):
            col_number = next_col + offset
            col_letter = _column_letter(col_number)
            # Prefix non-empty values with ' to prevent Google Sheets from
            # interpreting SSNs and phone numbers as formulas or numbers
            data_cells = [[f"'{val}" if val else ""] for val in col_values]
            worksheet.update(
                range_name=f"{col_letter}1:{col_letter}{len(labels)}",
                values=data_cells,
                value_input_option="USER_ENTERED",
            )

        submission_number = next_col - 1   # Column B = submission 1

    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
        submission_number,
        len(col_value_sets),
    )


def try_append_credit_google_row(values: dict[str, str]) -> dict[str, object]:
    """
    Safe wrapper around append_credit_google_row().
    Returns a result dict instead of raising exceptions.

    Returns:
        {
            "ok": bool,
            "skipped": bool,         # True if sync is disabled in .env
            "url": str,              # Sheet URL on success
            "row": int,              # Submission number
            "columns": int,          # Number of columns written
            "message": str,          # Human-readable result message
            "error": str,            # Error message on failure
        }
    """
    load_credit_env()
    if not _google_sheet_sync_enabled():
        return {"ok": False, "skipped": True, "message": "Google Sheet sync disabled"}
    try:
        sheet_url, submission_number, saved_columns = append_credit_google_row(values)
        detail = f"{saved_columns} columns" if saved_columns > 1 else f"submission #{submission_number}"
        return {
            "ok":      True,
            "url":     sheet_url,
            "row":     submission_number,
            "columns": saved_columns,
            "message": f"Done - Google Sheet {detail} saved",
        }
    except Exception as exc:
        return {
            "ok":      False,
            "error":   str(exc),
            "message": f"Done - Google Sheet not saved: {exc}",
        }


def clear_credit_sheet_data() -> dict[str, object]:
    """
    Clear all submission data from all 4 configured sheets (columns B onwards).
    Column A (field labels) is preserved.
    Requires correct password from the frontend (handled in web_app.py).

    Returns:
        {"ok": True, "cleared": N, "sheet_ids": [...], "message": "..."}
    """
    load_credit_env()
    credentials_path = _credentials_path()

    try:
        gspread = importlib.import_module("gspread")
    except ImportError as exc:
        raise ImportError("gspread not installed. Run: pip install gspread") from exc

    cleared: list[str] = []
    labels = _headers()

    with _GSHEETS_LOCK:
        gc = gspread.service_account(filename=str(credentials_path))
        for sheet_id in SHEET_CLEAR_IDS:
            spreadsheet = gc.open_by_key(sheet_id)
            worksheet   = spreadsheet.sheet1
            _ensure_col_a_labels(spreadsheet, worksheet, labels)
            if worksheet.col_count >= 2 and worksheet.row_count >= 1:
                last_col = _column_letter(worksheet.col_count)
                worksheet.batch_clear([f"B1:{last_col}{worksheet.row_count}"])
            cleared.append(sheet_id)

    return {
        "ok":       True,
        "cleared":  len(cleared),
        "sheet_ids": cleared,
        "message":  f"Cleared Sheet 1 data from {len(cleared)} Google Sheets",
    }
