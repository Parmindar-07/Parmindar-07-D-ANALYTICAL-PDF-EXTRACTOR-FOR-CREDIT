"""
================================================================================
  validators.py — AI JSON Output Validator & Context Repairer
================================================================================

  PURPOSE:
    After the AI returns a JSON dict, this module:
      1. Normalizes all field values (EIN digits, SSN format, state codes, DOB)
      2. Uses raw OCR text as a "context window" to repair misassigned numbers
      3. Removes phantom/duplicate owners
      4. Falls back owner address to business address when owner address is missing

  KEY REPAIR FUNCTIONS:
    _repair_ein_from_context      — Finds best 9-digit EIN candidate by proximity scoring
    _repair_owner_ssns_from_context — Assigns SSNs to correct owners by section position
    _repair_owner_dobs_from_context — Fills missing DOBs from unlabeled date candidates
    _repair_addresses_from_context  — Extracts City/State/ZIP groups and binds to entities
    _repair_swapped_addresses       — Detects and corrects business↔owner address swaps

  SCORING ALGORITHM:
    Each candidate 9-digit number gets a score based on:
      +50 if labeled as EIN/SSN
      -35 if labeled as the opposite type
      ±N  based on proximity to known owner/business positions
      -80 if inside an owner section (for EIN candidates)
      +45 if inside an owner section (for SSN candidates)
================================================================================
"""

from __future__ import annotations

import re

from ai.schemas.extraction_schema import (
    BUSINESS_KEYS,
    FIXED_BUSINESS_EMAIL,
    FIXED_BUSINESS_PHONE,
    OWNER_KEYS,
)
from .text_cleaner import clean_output, normalize_label


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — State Code Lookups
# ══════════════════════════════════════════════════════════════════════════════

STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "IA", "ID",
    "IL", "IN", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN", "MO", "MS", "MT",
    "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY", "DC",
    "AS", "GU", "MP", "PR", "TT", "VI",   # US territories
}

STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "AMERICAN SAMOA": "AS", "CALIFORNIA": "CA", "COLORADO": "CO",
    "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL", "GEORGIA": "GA", "GUAM": "GU", "HAWAII": "HI",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
    "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME",
    "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI",
    "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO",
    "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND",
    "NORTHERN MARIANA ISLANDS": "MP", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "PUERTO RICO": "PR",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "TRUST TERRITORIES": "TT",
    "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA",
    "VIRGIN ISLANDS": "VI", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY",
}

STATE_FULL_NAMES = {code: name for name, code in STATE_NAMES.items()}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Label Hint Sets (used for candidate scoring)
# ══════════════════════════════════════════════════════════════════════════════

# Field labels that are value-placeholder strings, not actual data
LABEL_VALUES = {
    "business name", "business address", "owner name", "city", "state",
    "zip", "zip code", "dob", "ssn", "phone", "fax", "email",
    "signature", "applicant", "owner", "federal tax id", "ein",
    "dba", "home address", "BUSINESS STREET ADDRESS",
}

# Keywords near a 9-digit number that suggest it's an EIN
EIN_LABEL_HINTS  = ("ein", "fein", "federal tax", "tax id", "taxid", "tin")
# Keywords near a 9-digit number that suggest it's an SSN
SSN_LABEL_HINTS  = ("ssn", "social security", "social sec", "ss", "ss#")
# Keywords near a date that suggest it's a date of birth
DOB_LABEL_HINTS  = ("dob", "date of birth", "birth date", "owner date of birth")

# Context keywords that identify business vs owner sections
BUSINESS_LABEL_HINTS    = ("business", "merchant", "company", "legal name", "dba")
OWNER_LABEL_HINTS       = ("owner", "principal", "guarantor", "signer", "member", "officer")
OWNER_SECTION_HINTS     = ("owner information", "owner principal information",
                           "principal information", "owner information #1")
OWNER_SECTION_END_HINTS = ("owner signature", "agreement", "business trade references",
                           "property information", "owner information #2")
NON_OWNER_CONTACT_HINTS = ("representative", "agent", "broker", "funding specialist",
                           "submitted by", "")
BUSINESS_SECTION_HINTS     = ("business information", "merchant information")
BUSINESS_SECTION_END_HINTS = ("owner information", "2nd owner information",
                              "owner principal information")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Low-level Field Formatters
# ══════════════════════════════════════════════════════════════════════════════

def _digits(value: object) -> str:
    """Extract only digit characters from any value."""
    return re.sub(r"\D", "", str(value or ""))


def _format_ein(value: object) -> str:
    """
    Return exactly 9 digits for EIN, or "" if fewer than 9 digits are present.
    Hyphens and spaces are stripped automatically.
    """
    digits = _digits(value)[:9]
    return digits if len(digits) == 9 else ""


def _format_ssn(value: object) -> str:
    """
    Format SSN as ###-##-#### from a 9-digit string.
    Returns "" if fewer than 9 digits are present.
    """
    digits = _digits(value)[:9]
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}" if len(digits) == 9 else ""


def _normalize_state(value: object) -> str:
    """
    Convert any state representation to a 2-letter US state code.
    Handles: "FL", "Florida", "FLORIDA", OCR noise like "F1" → "FL".
    Returns "" if no valid state code is found.
    """
    raw = clean_output(value).upper()
    raw = re.sub(r"[^A-Z ]", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    if raw in STATE_CODES:
        return raw
    # Try extracting a 2-letter code from anywhere in the string
    match = re.search(r"\b[A-Z]{2}\b", raw)
    if match and match.group(0) in STATE_CODES:
        return match.group(0)
    # Try full state name lookup
    for name, code in STATE_NAMES.items():
        if re.search(rf"\b{name}\b", raw):
            return code
    return STATE_NAMES.get(raw, "")


def _normalize_zip(value: object) -> str:
    """Extract the first valid 5-digit (or 5+4) US ZIP from a string."""
    match = re.search(r"\b\d{5}(?:-\d{4})?\b", str(value or ""))
    return match.group(0) if match else ""


def _normalize_dob(value: object) -> str:
    """
    Normalize a date of birth string to MM-DD-YYYY format.
    Handles:
      - MM/DD/YYYY  → MM-DD-YYYY
      - YYYY/MM/DD  → MM-DD-YYYY
      - 2-digit year → 19xx or 20xx (cutoff at 30)
    Returns "" if no valid date pattern is found.
    """
    text = clean_output(value).replace(".", "/").replace("-", "/")

    # ISO format: YYYY/MM/DD
    iso = re.search(r"\b(\d{4})/(\d{1,2})/(\d{1,2})\b", text)
    if iso:
        year, month, day = iso.groups()
        return f"{int(month):02d}-{int(day):02d}-{year}"

    # US format: MM/DD/YY or MM/DD/YYYY
    match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text)
    if not match:
        return ""
    month, day, year = match.groups()
    if len(year) == 2:
        year = f"19{year}" if int(year) > 30 else f"20{year}"
    return f"{int(month):02d}-{int(day):02d}-{year}"


def _safe_text(value: object) -> str:
    """
    Return the cleaned value only if it is actual data, not a label placeholder.
    Prevents field labels like "Business Name" from ending up as field values.
    """
    text = clean_output(value)
    return "" if normalize_label(text) in LABEL_VALUES else text


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Context Line Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _context_lines(raw_text: str | None) -> list[str]:
    """Split raw OCR text into a list of non-empty cleaned lines."""
    if not raw_text:
        return []
    return [
        clean_output(line)
        for line in str(raw_text).replace("\r", "\n").splitlines()
        if clean_output(line)
    ]


def _find_line_position(lines: list[str], value: object) -> int | None:
    """
    Find the index of the line in `lines` that best matches `value`.
    Used to estimate where a field value appears in the document.
    Returns None if not found.
    """
    needle = normalize_label(value)
    if not needle:
        return None
    for index, line in enumerate(lines):
        normalized = normalize_label(line)
        if needle in normalized or normalized in needle:
            return index
    # Try word-by-word match (at least 2 of first 3 words must match)
    words = [word for word in needle.split() if len(word) > 2]
    if len(words) >= 2:
        for index, line in enumerate(lines):
            normalized = normalize_label(line)
            if all(word in normalized for word in words[:3]):
                return index
    return None


def _first_position(*positions: int | None) -> int | None:
    """Return the first non-None position from a sequence of candidates."""
    for position in positions:
        if position is not None:
            return position
    return None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Owner Section Range Detection
# ══════════════════════════════════════════════════════════════════════════════

def _owner_section_ranges(lines: list[str]) -> list[tuple[int, int]]:
    """
    Find line ranges that belong to owner information sections.
    Returns list of (start, end) tuples — end is exclusive.
    A number found inside one of these ranges is likely SSN, not EIN.
    """
    ranges = []
    starts = [
        index for index, line in enumerate(lines)
        if any(hint in normalize_label(line) for hint in OWNER_SECTION_HINTS)
    ]
    for start in starts:
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if any(hint in normalize_label(lines[index]) for hint in OWNER_SECTION_END_HINTS):
                end = index
                break
        ranges.append((start, end))
    return ranges


def _in_ranges(line_index: int, ranges: list[tuple[int, int]]) -> bool:
    """Return True if line_index falls within any of the given ranges."""
    return any(start <= line_index < end for start, end in ranges)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — 9-Digit Number Candidate Collection
# ══════════════════════════════════════════════════════════════════════════════

def _number_context_candidates(lines: list[str]) -> list[dict[str, object]]:
    """
    Scan all lines for 9-digit numbers (formatted as EIN or SSN).
    For each match, record contextual signals:
      - is_ein      : EIN-related labels nearby
      - is_ssn      : SSN-related labels nearby
      - business_score / owner_score : keyword counts in surrounding lines
      - line        : line index (used for proximity scoring)

    Pattern matches: ##-####### | ###-##-#### | ######### (raw 9 digits)
    """
    candidates: list[dict[str, object]] = []
    pattern = re.compile(r"(?<!\d)(?:\d{2}-\d{7}|\d{3}-\d{2}-\d{4}|\d{9})(?!\d)")

    for index, line in enumerate(lines):
        for match in pattern.finditer(line):
            digits = _digits(match.group(0))
            if len(digits) != 9:
                continue

            # Look at 4 lines before and 4 lines after for context
            window          = " ".join(lines[max(0, index - 4):index + 5])
            normalized_window = normalize_label(window)
            normalized_line   = normalize_label(line)

            candidates.append({
                "digits":         digits,
                "text":           match.group(0),
                "line":           index,
                "is_ein":         any(hint in normalized_window for hint in EIN_LABEL_HINTS),
                "is_ssn":         any(hint in normalized_window for hint in SSN_LABEL_HINTS),
                "business_score": sum(1 for h in BUSINESS_LABEL_HINTS if h in normalized_window),
                "owner_score":    sum(1 for h in OWNER_LABEL_HINTS    if h in normalized_window),
                "line_text":      normalized_line,
            })
    return candidates


def _candidate_distance(candidate: dict[str, object], position: int | None) -> int:
    """Line distance between a candidate and a known field position. 9999 if unknown."""
    if position is None:
        return 9999
    return abs(int(candidate["line"]) - position)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — Context-Based EIN Repair
# ══════════════════════════════════════════════════════════════════════════════

def _repair_ein_from_context(
    business: dict[str, str],
    lines: list[str],
    candidates: list[dict[str, object]],
) -> None:
    """
    Score every 9-digit candidate and pick the best EIN for the business.
    Overwrites business["ein_no"] only when the best score >= 20.

    Scoring heuristics:
      +50  labeled as EIN
      -35  labeled as SSN
      -80  inside an owner section (strong negative signal)
      +10  NOT inside an owner section
      +8   per business-context keyword in surrounding lines
      -3   per owner-context keyword
      +N   proximity bonus (max +18 at distance 0)
      +12  matches AI's existing EIN value (confirmation bonus)
    """
    if not lines or not candidates:
        return

    # Anchor position: where does the business info appear in the document?
    business_pos = _first_position(
        _find_line_position(lines, business.get("business_name", "")),
        _find_line_position(lines, business.get("dba_name", "")),
        _find_line_position(lines, business.get("business_street", "")),
    )
    current_digits = _digits(business.get("ein_no", ""))
    owner_ranges   = _owner_section_ranges(lines)

    scored: list[tuple[int, dict[str, object]]] = []
    for candidate in candidates:
        score = 0
        if candidate["is_ein"]:               score += 50
        if candidate["is_ssn"]:               score -= 35
        if _in_ranges(int(candidate["line"]), owner_ranges):
            score -= 80   # Very strong negative — numbers in owner section are SSNs
        else:
            score += 10

        score += int(candidate["business_score"]) * 8
        score -= int(candidate["owner_score"])    * 3

        distance = _candidate_distance(candidate, business_pos)
        if distance <= 8:
            score += 18 - distance   # Proximity bonus (linear decay)

        if current_digits and candidate["digits"] == current_digits:
            score += 12   # AI already found this — trust it a bit more

        scored.append((score, candidate))

    if not scored:
        return
    score, best = max(scored, key=lambda item: item[0])
    if score >= 20:
        business["ein_no"] = _format_ein(best["digits"])


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — Context-Based SSN Repair
# ══════════════════════════════════════════════════════════════════════════════

def _repair_owner_ssns_from_context(
    owners: list[dict[str, str]],
    lines: list[str],
    candidates: list[dict[str, object]],
) -> None:
    """
    For each owner that is missing or has a low-confidence SSN, find the
    best matching 9-digit number from the owner-section candidates.

    Scoring heuristics:
      +50  labeled as SSN
      -35  labeled as EIN
      +45  inside an owner section
      +8   per owner-context keyword
      +22  proximity to owner anchor (name/address line)
      +35  falls after this owner's anchor and before the next owner's anchor
      -30  candidate line already assigned to another owner
    """
    if not lines or not owners or not candidates:
        return

    owner_ranges = _owner_section_ranges(lines)

    # Pre-filter: only candidates that look like SSNs or are in owner sections
    ssn_candidates = [
        c for c in candidates
        if (
            c["is_ssn"]
            or (int(c["owner_score"]) > int(c["business_score"]))
            or _in_ranges(int(c["line"]), owner_ranges)
        )
    ]
    if not ssn_candidates:
        return

    # Pre-compute each owner's anchor line position
    owner_positions = [
        _first_position(
            _find_line_position(lines, o.get("owner_name", "")),
            _find_line_position(lines, o.get("owner_street", "")),
        )
        for o in owners
    ]

    used_lines: set[int] = set()   # Prevent two owners from sharing a candidate

    for owner_index, owner in enumerate(owners):
        owner_pos = _first_position(
            _find_line_position(lines, owner.get("owner_name", "")),
            _find_line_position(lines, owner.get("owner_street", "")),
        )
        # Next owner's position is the exclusive upper bound for this owner's SSN
        next_positions = [p for p in owner_positions[owner_index + 1:] if p is not None]
        next_owner_pos = min(next_positions) if next_positions else None

        current_digits = _digits(owner.get("owner_ssn", ""))
        scored: list[tuple[int, dict[str, object]]] = []

        for candidate in ssn_candidates:
            candidate_line = int(candidate["line"])
            score = 0

            if candidate["is_ssn"]:   score += 50
            if candidate["is_ein"]:   score -= 35
            if _in_ranges(candidate_line, owner_ranges):  score += 45
            score += int(candidate["owner_score"]) * 8

            distance = _candidate_distance(candidate, owner_pos)
            if distance <= 10:
                score += 22 - distance   # Proximity bonus

            # Strongest signal: candidate falls between this owner and the next
            if (
                owner_pos is not None
                and candidate_line >= owner_pos
                and (next_owner_pos is None or candidate_line < next_owner_pos)
            ):
                score += 35
            elif candidate_line in used_lines:
                score -= 30   # Already claimed by another owner

            if current_digits and candidate["digits"] == current_digits:
                score += 3    # Minor confirmation bonus

            scored.append((score, candidate))

        if not scored:
            continue
        score, best = max(scored, key=lambda item: item[0])
        if score >= 18:
            owner["owner_ssn"] = _format_ssn(best["digits"])
            used_lines.add(int(best["line"]))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — Context-Based DOB Repair
# ══════════════════════════════════════════════════════════════════════════════

def _date_context_candidates(lines: list[str]) -> list[dict[str, object]]:
    """Collect all date-like strings from the document with contextual signals."""
    candidates = []
    owner_ranges = _owner_section_ranges(lines)
    pattern = re.compile(
        r"(?<!\d)(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})(?!\d)"
    )
    for index, line in enumerate(lines):
        for match in pattern.finditer(line):
            window     = " ".join(lines[max(0, index - 2):index + 2])
            normalized = normalize_label(window)
            candidates.append({
                "value":              match.group(0),
                "line":               index,
                "is_dob":             any(h in normalized for h in DOB_LABEL_HINTS),
                "is_owner":           _in_ranges(index, owner_ranges),
                "is_signature":       "signature" in normalized,
                "is_business_start":  "business start" in normalized,
            })
    return candidates


def _repair_owner_dobs_from_context(owners: list[dict[str, str]], lines: list[str]) -> None:
    """
    Fill missing owner DOBs by scoring date candidates from the raw text.
    Skips signature dates and business start dates.
    Minimum score threshold: 40 (requires strong DOB or owner-section signal).
    """
    if not lines or not owners:
        return
    candidates = _date_context_candidates(lines)
    if not candidates:
        return

    for owner in owners:
        if owner.get("owner_dob"):    # Skip if AI already found a DOB
            continue
        owner_pos = _first_position(
            _find_line_position(lines, owner.get("owner_name", "")),
            _find_line_position(lines, owner.get("owner_street", "")),
        )
        scored = []
        for candidate in candidates:
            if candidate["is_signature"] or candidate["is_business_start"]:
                continue    # These dates are never DOBs
            score = 0
            if candidate["is_dob"]:   score += 60
            if candidate["is_owner"]: score += 40
            distance = _candidate_distance(candidate, owner_pos)
            if distance <= 12:        score += 18 - distance
            scored.append((score, candidate))

        if scored:
            score, best = max(scored, key=lambda item: item[0])
            if score >= 40:
                owner["owner_dob"] = _normalize_dob(best["value"])


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — Address Context Repair
# ══════════════════════════════════════════════════════════════════════════════

def _sort_owners_by_context(
    owners: list[dict[str, str]], lines: list[str]
) -> list[dict[str, str]]:
    """
    Re-order owners by their appearance position in the raw document.
    Ensures Owner 1 = first person appearing in the document, etc.
    """
    if not lines:
        return owners
    missing = len(lines) + 100
    indexed = []
    for original_index, owner in enumerate(owners):
        position = _first_position(
            _find_line_position(lines, owner.get("owner_name", "")),
            _find_line_position(lines, owner.get("owner_street", "")),
        )
        indexed.append((
            position if position is not None else missing + original_index,
            original_index,
            owner,
        ))
    indexed.sort(key=lambda item: (item[0], item[1]))
    return [owner for _, _, owner in indexed]


def _address_groups_from_context(lines: list[str]) -> list[dict[str, object]]:
    """
    Find "City, State, ZIP" patterns in the document and reconstruct
    full address groups (street + city + state + zip).

    The line immediately before a City/State/ZIP match is treated as the street,
    provided it contains at least one digit (address number check).
    """
    groups = []
    owner_ranges = _owner_section_ranges(lines)
    # Matches: City, ST, 12345 or City, Full State Name, 12345
    pattern = re.compile(
        r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z .-]+),\s*([A-Za-z]{2}|[A-Za-z ]+),\s*"
        r"(\d{5}(?:-\d{4})?)(?![A-Za-z0-9])"
    )
    for index, line in enumerate(lines):
        match = pattern.search(line)
        if not match or index == 0:
            continue
        street = clean_output(lines[index - 1])
        if not re.search(r"\d", street):   # Streets must have a number
            continue
        city, state, zip_code = match.groups()
        groups.append({
            "street": street,
            "city":   clean_output(city),
            "state":  _normalize_state(state),
            "zip":    zip_code,
            # True if this address appears inside an owner section
            "owner":  _in_ranges(index, owner_ranges) or _in_ranges(index - 1, owner_ranges),
        })
    return groups


def _apply_address_group(target: dict, group: dict, prefix: str) -> None:
    """Write a detected address group into a business or owner dict."""
    target[f"{prefix}_street"] = group.get("street", "")
    target[f"{prefix}_city"]   = group.get("city",   "")
    target[f"{prefix}_state"]  = group.get("state",  "")
    target[f"{prefix}_zip"]    = group.get("zip",    "")


def _repair_addresses_from_context(
    business: dict[str, str],
    owners: list[dict[str, str]],
    lines: list[str],
) -> None:
    """
    Use detected City/State/ZIP groups to fill missing business and owner addresses.
    Groups flagged as "owner" are assigned to owners, others to business.
    """
    groups = _address_groups_from_context(lines)
    if not groups:
        return
    business_groups = [g for g in groups if not g.get("owner")]
    owner_groups    = [g for g in groups if g.get("owner")]
    if business_groups:
        _apply_address_group(business, business_groups[0], "business")
    for owner, group in zip(owners, owner_groups):
        _apply_address_group(owner, group, "owner")


def _repair_swapped_addresses_from_context(
    business: dict[str, str],
    owners: list[dict[str, str]],
    lines: list[str],
) -> None:
    """
    Detect and correct business↔owner address swaps.
    This happens when OCR reads pages out of order and the AI misassigns
    the owner's home address as the business street address.

    Detection: business_street line falls inside an owner section range,
               while an owner's street falls outside — these are swapped.
    """
    owner_ranges = _owner_section_ranges(lines)
    if not owner_ranges or not owners:
        return
    business_line = _find_line_position(lines, business.get("business_street", ""))
    if business_line is None or not _in_ranges(business_line, owner_ranges):
        return   # Business address is correctly placed — no swap needed
    for owner in owners:
        owner_line = _find_line_position(lines, owner.get("owner_street", ""))
        if owner_line is None or _in_ranges(owner_line, owner_ranges):
            continue   # This owner's address is also inside owner section — not the swap
        # Swap all address subfields between business and this owner
        for bk, ok in (
            ("business_street", "owner_street"),
            ("business_city",   "owner_city"),
            ("business_state",  "owner_state"),
            ("business_zip",    "owner_zip"),
        ):
            business[bk], owner[ok] = owner.get(ok, ""), business.get(bk, "")
        return   # Only fix the first detected swap


def _repair_with_raw_context(
    business: dict[str, str],
    owners: list[dict[str, str]],
    raw_text: str | None,
) -> list[dict[str, str]]:
    """
    Master repair function — runs all context-based repairs in the correct order.

    Order matters:
      1. Sort owners by document position (so proximity scoring is correct)
      2. Fill DOBs before addresses (DOB scoring uses owner position)
      3. Fill addresses before SSN/EIN (addresses define "owner section" boundaries)
      4. EIN repair after address repair (owner section ranges are now accurate)
      5. SSN repair last (uses final owner positions)
    """
    lines = _context_lines(raw_text)
    if not lines:
        return owners
    candidates = _number_context_candidates(lines)
    _repair_ein_from_context(business, lines, candidates)
    owners = _sort_owners_by_context(owners, lines)
    _repair_owner_dobs_from_context(owners, lines)
    _repair_addresses_from_context(business, owners, lines)
    _repair_swapped_addresses_from_context(business, owners, lines)
    _repair_owner_ssns_from_context(owners, lines, candidates)
    return owners


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — Owner Deduplication & Phantom Removal
# ══════════════════════════════════════════════════════════════════════════════

def _is_non_owner_contact_name(owner: dict[str, str], lines: list[str]) -> bool:
    """
    Return True if the owner's name appears near labels that indicate
    this person is NOT a business owner (e.g., broker, agent, rep).
    """
    name_pos = _find_line_position(lines, owner.get("owner_name", ""))
    if name_pos is None:
        return False
    window     = " ".join(lines[max(0, name_pos - 3):name_pos + 4])
    normalized = normalize_label(window)
    return any(hint in normalized for hint in NON_OWNER_CONTACT_HINTS)


def _remove_non_owner_contacts(
    owners: list[dict[str, str]], lines: list[str]
) -> list[dict[str, str]]:
    """
    Remove owners that are actually ISO/broker contacts, not business owners.
    Always keeps Owner 1 (index 0) regardless.
    """
    if not lines or len(owners) <= 1:
        return owners
    kept = owners[:1]   # Owner 1 is always kept
    for owner in owners[1:]:
        if _is_non_owner_contact_name(owner, lines):
            continue
        kept.append(owner)
    return kept


def _remove_phantom_owners(owners: list[dict[str, str]]) -> list[dict[str, str]]:
    """
    Remove duplicate or empty phantom owners:
      1. If any owner has a name, drop all unnamed owners
      2. Deduplicate by (ssn, normalized_name, normalized_street) identity
    """
    # Step 1: Drop unnamed owners when named ones exist
    named = [o for o in owners if normalize_label(o.get("owner_name", ""))]
    if named:
        owners = named

    # Step 2: Deduplicate by identity tuple
    seen: set[tuple[str, str, str]] = set()
    cleaned: list[dict[str, str]] = []
    for owner in owners:
        identity = (
            owner.get("owner_ssn", ""),
            normalize_label(owner.get("owner_name", "")),
            normalize_label(owner.get("owner_street", "")),
        )
        if identity in seen:
            continue
        seen.add(identity)
        cleaned.append(owner)
    return cleaned


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 12 — Field Normalization
# ══════════════════════════════════════════════════════════════════════════════

def _business_info(data: dict[str, object]) -> dict[str, str]:
    """
    Extract and normalize all business fields from AI JSON.
    - Strips label contamination (e.g., "Business Name" as a value)
    - Forces fixed phone/email
    - Normalizes state code, ZIP, and EIN
    """
    raw      = data.get("business_info") if isinstance(data, dict) else {}
    raw      = raw if isinstance(raw, dict) else {}
    business = {key: _safe_text(raw.get(key, "")) for key in BUSINESS_KEYS}

    business["business_phone"] = FIXED_BUSINESS_PHONE   # Always override
    business["business_email"] = FIXED_BUSINESS_EMAIL   # Always override
    business["business_state"] = _normalize_state(business.get("business_state", ""))
    business["business_zip"]   = _normalize_zip(business.get("business_zip", ""))
    business["ein_no"]         = _format_ein(business.get("ein_no", ""))
    return business


def _owner_info(owner: object) -> dict[str, str]:
    """Extract and normalize all fields for a single owner."""
    raw  = owner if isinstance(owner, dict) else {}
    item = {key: _safe_text(raw.get(key, "")) for key in OWNER_KEYS}
    item["owner_state"] = _normalize_state(item.get("owner_state", ""))
    item["owner_zip"]   = _normalize_zip(item.get("owner_zip", ""))
    item["owner_dob"]   = _normalize_dob(item.get("owner_dob", ""))
    item["owner_ssn"]   = _format_ssn(item.get("owner_ssn", ""))
    return item


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 13 — Main Public Entry Point
# ══════════════════════════════════════════════════════════════════════════════

def validate_ai_json(
    data: dict[str, object],
    raw_text: str | None = None,
) -> dict[str, object]:
    """
    Full validation and repair pipeline for AI-extracted JSON.

    Steps:
      1. Normalize all business fields
      2. Normalize each owner (max 4), skip empty/duplicate owners
      3. Fill missing owner address from business address (fallback)
      4. Run context-based repairs using raw OCR text (if provided)
      5. Remove ISO/broker names mistakenly added as owners
      6. Deduplicate phantom owners

    Args:
        data:     Raw dict returned by the AI provider.
        raw_text: Original OCR text (used as context for repairs).

    Returns:
        Cleaned dict with "business_info" and "owners" keys.
    """
    business   = _business_info(data)
    owners_raw = data.get("owners") if isinstance(data, dict) else []
    owners_raw = owners_raw if isinstance(owners_raw, list) else []

    owners: list[dict[str, str]] = []
    seen:   set[tuple[str, str]] = set()

    for raw_owner in owners_raw[:4]:   # Hard limit: max 4 owners
        owner = _owner_info(raw_owner)

        # Skip completely empty owner entries
        if not any(owner.values()):
            continue

        # Deduplicate by (name, SSN) identity
        identity = (normalize_label(owner.get("owner_name", "")), owner.get("owner_ssn", ""))
        if identity in seen:
            continue
        seen.add(identity)

        # Address fallback: use business address when owner has no address
        if not owner.get("owner_street"):
            owner["owner_street"] = business.get("business_street", "")
            owner["owner_city"]   = owner.get("owner_city")  or business.get("business_city", "")
            owner["owner_state"]  = owner.get("owner_state") or business.get("business_state", "")
            owner["owner_zip"]    = owner.get("owner_zip")   or business.get("business_zip", "")

        owners.append(owner)

    # Context-based repairs using raw OCR text
    owners = _repair_with_raw_context(business, owners, raw_text)
    owners = _remove_non_owner_contacts(owners, _context_lines(raw_text))
    owners = _remove_phantom_owners(owners)

    return {"business_info": business, "owners": owners}


# ── Frontend mapping helper (also used by web_app.py) ────────────────────────

def _plain_text(value: object) -> str:
    return "" if value is None else str(value)


def ai_json_to_frontend_values(data: dict[str, object]) -> dict[str, str]:
    """
    Flatten the nested AI JSON dict into the flat key-value dict
    that the frontend HTML form expects.

    Owner keys follow this pattern:
      Owner 1 → ownerName, ownerDob, ownerAddress, ownerCity, ownerState, ownerZip, ssn
      Owner 2 → owner2Name, owner2Dob, owner2Address, owner2City, owner2State, owner2Zip, owner2Ssn
      Owner 3 → owner3Name ... owner3Ssn
      Owner 4 → owner4Name ... owner4Ssn
    """
    business = data.get("business_info") if isinstance(data, dict) else {}
    business = business if isinstance(business, dict) else {}

    values: dict[str, str] = {
        "businessName":    _plain_text(business.get("business_name", "")),
        "businessDbaName": _plain_text(business.get("dba_name", "")),
        "businessAddress": _plain_text(business.get("business_street", "")),
        "businessCity":    _plain_text(business.get("business_city", "")),
        "businessState":   _plain_text(business.get("business_state", "")),
        "businessZip":     _plain_text(business.get("business_zip", "")),
        "businessPhone":   _plain_text(business.get("business_phone", "")),
        "businessEmail":   _plain_text(business.get("business_email", "")),
        "einNumber":       _plain_text(business.get("ein_no", "")),
    }

    owners = data.get("owners") if isinstance(data, dict) else []
    owners = owners if isinstance(owners, list) else []

    for index, owner in enumerate(owners[:4], start=1):
        owner  = owner if isinstance(owner, dict) else {}
        prefix = "owner" if index == 1 else f"owner{index}"
        values[f"{prefix}Name"]    = _plain_text(owner.get("owner_name", ""))
        values[f"{prefix}Dob"]     = _plain_text(owner.get("owner_dob", ""))
        values[f"{prefix}Address"] = _plain_text(owner.get("owner_street", ""))
        values[f"{prefix}City"]    = _plain_text(owner.get("owner_city", ""))
        values[f"{prefix}State"]   = _plain_text(owner.get("owner_state", ""))
        values[f"{prefix}Zip"]     = _plain_text(owner.get("owner_zip", ""))
        values["ssn" if index == 1 else f"owner{index}Ssn"] = _plain_text(owner.get("owner_ssn", ""))

    return values
