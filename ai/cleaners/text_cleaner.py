"""
================================================================================
  text_cleaner.py — OCR Text Cleaning & Noise Removal
================================================================================

  PURPOSE:
    Removes garbage from raw OCR output before sending to AI.
    Poor OCR quality causes AI hallucination — cleaner input = better output.

  WHAT IT REMOVES:
    - DocuSign / TCPDF audit trail lines
    - URLs, IP addresses, UUIDs
    - Single characters and pure-symbol lines
    - Lines exceeding MAX_LINE_LENGTH (usually long garbage)
    - Lines matching user-configured blocklist (raw_text_remove_lines.txt)

  BLOCKLIST FILE FORMAT (raw_text_remove_lines.txt):
    Lines starting with # are comments.
    Plain lines        → exact normalized match
    "contains: word"  → remove if normalized line contains word
    "regex: pattern"  → remove if regex matches (case-insensitive)

  ENVIRONMENT VARIABLES:
    RAW_TEXT_MAX_LINE_LENGTH  — max chars per line before removal (default 220)
    RAW_TEXT_BLOCKLIST_PATH   — path to blocklist file (default raw_text_remove_lines.txt)
================================================================================
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────
MAX_LINE_LENGTH = int(os.getenv("RAW_TEXT_MAX_LINE_LENGTH", "220"))
BLOCKLIST_PATH  = Path(os.getenv("RAW_TEXT_BLOCKLIST_PATH", "raw_text_remove_lines.txt"))


# ── Text normalization helpers ─────────────────────────────────────────────────

def clean_output(value: object) -> str:
    """
    Strips leading/trailing punctuation, collapses whitespace, and removes
    spaces before punctuation marks like commas and colons.

    Examples:
        "  : Business Name  "  →  "Business Name"
        "City ,  State"        →  "City, State"
    """
    text = re.sub(r"\s+", " ", str(value or ""))
    text = re.sub(r"\s+([,.:;])", r"\1", text)
    return re.sub(r"^[\s:.\-]+|[\s:.\-]+$", "", text).strip()


def normalize_label(value: object) -> str:
    """
    Converts a label to lowercase alphanumeric with single spaces.
    Also corrects known OCR typos in common field labels.

    Used to compare labels in a typo-tolerant way.

    Examples:
        "Bus ness Name:"   →  "business name"
        "State 2"          →  "state"
        "Ownersh p"        →  "ownership"
    """
    # Lowercase, keep only alphanumeric and spaces
    normalized = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())).strip()

    # Fix common OCR split-word errors in field label names
    ocr_typo_fixes = {
        "bus ness":  "business",
        "busi ness": "business",
        "lega":      "legal",
        "federa":    "federal",
        "fu name":   "full name",
        "ownersh p": "ownership",
        "state 2":   "state",
    }
    for bad, good in ocr_typo_fixes.items():
        normalized = re.sub(rf"\b{re.escape(bad)}\b", good, normalized)

    return re.sub(r"\s+", " ", normalized).strip()


# ── Blocklist loading ──────────────────────────────────────────────────────────

def _load_rules() -> tuple[set[str], list[str], list[str]]:
    """
    Reads raw_text_remove_lines.txt and returns three rule categories:
        exact    — normalized strings that must match exactly
        contains — normalized substrings that must appear in the line
        regexes  — raw regex patterns (case-insensitive match)
    """
    exact: set[str]    = set()
    contains: list[str] = []
    regexes: list[str]  = []

    if not BLOCKLIST_PATH.exists():
        return exact, contains, regexes

    for raw_line in BLOCKLIST_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
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


# ── Main cleaning function ────────────────────────────────────────────────────

def clean_ocr_text(raw_text: str) -> str:
    """
    Removes junk lines from raw OCR output.

    Removal criteria (in order):
      1. Empty after clean_output()
      2. Exceeds MAX_LINE_LENGTH characters
      3. Single character (letter or digit) or pure punctuation-only line
      4. Contains DocuSign / TCPDF audit trail keywords
      5. Contains URLs or UUID patterns
      6. Matches blocklist exact / contains / regex rules

    Args:
        raw_text: Raw multiline OCR output string.

    Returns:
        Cleaned multiline string with max 2 consecutive blank lines.
    """
    exact, contains, regexes = _load_rules()
    lines: list[str] = []

    for raw_line in str(raw_text or "").replace("\r", "\n").splitlines():
        line       = clean_output(raw_line)
        normalized = normalize_label(line)

        # Rule 1: Skip empty lines
        if not line:
            continue

        # Rule 2: Skip very long lines (usually garbled OCR or base64 blobs)
        if MAX_LINE_LENGTH > 0 and len(line) > MAX_LINE_LENGTH:
            continue

        # Rule 3: Skip single-character lines and pure-symbol lines
        if re.fullmatch(r"[A-Za-z]|\d|[-_*/\\|.,;:()\[\]{}]+", line):
            continue

        # Rule 4: Skip DocuSign / TCPDF / audit trail noise
        if re.search(
            r"\b(docusign|tcpdf|audit trail|authorization|ip address|timestamp|page \d+ of \d+)\b",
            line, re.I
        ):
            continue

        # Rule 5: Skip URLs and UUIDs (e.g. "https://..." or "A1B2C3D4-E5F6-...")
        if re.search(r"\bhttps?://|www\.|[A-F0-9]{8}-[A-F0-9-]{8,}\b", line, re.I):
            continue

        # Rule 6a: Exact blocklist match
        if normalized in exact:
            continue

        # Rule 6b: Contains blocklist match
        if any(marker and marker in normalized for marker in contains):
            continue

        # Rule 6c: Regex blocklist match
        remove = False
        for pattern in regexes:
            try:
                remove = bool(re.search(pattern, line, re.I))
            except re.error:
                remove = False
            if remove:
                break
        if remove:
            continue

        lines.append(line)

    # Collapse 3+ consecutive blank lines to 2
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
