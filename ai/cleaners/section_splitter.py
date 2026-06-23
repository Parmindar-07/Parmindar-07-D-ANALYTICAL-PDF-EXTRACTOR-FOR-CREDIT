"""
================================================================================
  section_splitter.py — OCR Text Section Organizer
================================================================================

  PURPOSE:
    After text_cleaner.py removes noise, this module groups the remaining
    lines into logical sections: business, owner, funding, signature.

    Organized sections help the AI correctly assign values (e.g., avoid
    putting business address in owner fields or vice versa).

  HOW IT WORKS:
    - Reads cleaned text line-by-line
    - Switches the "current section" when it detects a section-header keyword
    - Outputs each section with a clear heading so the AI prompt is structured

  SECTION DETECTION KEYWORDS:
    business   → "business", "merchant", "company", "ein", "federal tax", "dba"
    owner      → "owner", "principal", "guarantor", "signer", "ssn", "date of birth"
    funding    → "funding", "capital", "advance", "loan", "revenue", "deposit"
    signature  → "signature", "print name", "date signed", "signed"
================================================================================
"""

from __future__ import annotations

from .text_cleaner import clean_output, normalize_label


# ── Section keyword maps ──────────────────────────────────────────────────────

_BUSINESS_TOKENS  = ("business", "merchant", "company", "ein", "federal tax", "dba")
_OWNER_TOKENS     = ("owner", "principal", "guarantor", "signer", "ssn", "date of birth", "home address")
_FUNDING_TOKENS   = ("funding", "capital", "advance", "loan", "revenue", "deposit")
_SIGNATURE_TOKENS = ("signature", "print name", "date signed", "signed")


# ── Core splitting logic ──────────────────────────────────────────────────────

def split_sections(cleaned_text: str) -> dict[str, str]:
    """
    Split a cleaned OCR text string into logical named sections.

    The function walks each line and tracks which section it belongs to
    based on keyword detection. Lines that don't trigger a section change
    stay in the current section.

    Args:
        cleaned_text: Output of text_cleaner.clean_ocr_text().

    Returns:
        Dict with keys: "business", "owner", "funding", "signature"
        (only sections that have at least one line are included).
    """
    sections: dict[str, list[str]] = {
        "business":  [],
        "owner":     [],
        "funding":   [],
        "signature": [],
    }
    current = "business"   # Default: start in business section

    for raw_line in str(cleaned_text or "").splitlines():
        line  = clean_output(raw_line)
        label = normalize_label(line)

        if not line:
            continue

        # Detect section transitions by checking keyword membership
        if any(token in label for token in _BUSINESS_TOKENS):
            current = "business"
        elif any(token in label for token in _OWNER_TOKENS):
            current = "owner"
        elif any(token in label for token in _FUNDING_TOKENS):
            current = "funding"
        elif any(token in label for token in _SIGNATURE_TOKENS):
            current = "signature"

        sections[current].append(line)

    # Return only non-empty sections as joined strings
    return {
        name: "\n".join(lines).strip()
        for name, lines in sections.items()
        if lines
    }


def useful_text_for_ai(cleaned_text: str) -> str:
    """
    Convert cleaned OCR text into a structured prompt-friendly string.

    Each section is prefixed with an uppercase heading so the AI prompt
    clearly shows where business data ends and owner data begins.

    Args:
        cleaned_text: Output of text_cleaner.clean_ocr_text().

    Returns:
        Multiline string with section headings, or the original cleaned_text
        if no sections were detected (fallback — better than sending nothing).

    Example output:
        BUSINESS SECTION
        Business Name: ABC Corp
        EIN: 12-3456789

        OWNER SECTION
        Owner Name: John Doe
        SSN: 123-45-6789
    """
    sections = split_sections(cleaned_text)

    ordered: list[str] = []
    for title, key in [
        ("BUSINESS SECTION",   "business"),
        ("OWNER SECTION",      "owner"),
        ("FUNDING SECTION",    "funding"),
        ("SIGNATURE SECTION",  "signature"),
    ]:
        value = sections.get(key, "")
        if value:
            ordered.append(f"{title}\n{value}")

    # Fallback: if no sections detected at all, send the raw cleaned text
    return "\n\n".join(ordered) if ordered else cleaned_text
