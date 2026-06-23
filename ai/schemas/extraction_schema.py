"""
================================================================================
  extraction_schema.py — JSON Output Schema for AI Extraction
================================================================================

  PURPOSE:
    Defines the exact JSON structure that every AI provider must return.
    The schema is injected into the prompt via {{JSON_SCHEMA}} placeholder
    so the AI knows exactly what fields to fill.

  FIXED VALUES:
    business_phone and business_email are always the same (TVT Capital).
    They are set here so every provider inherits them automatically.

  USAGE:
    from ai.schemas.extraction_schema import EXTRACTION_SCHEMA, BUSINESS_KEYS, OWNER_KEYS
================================================================================
"""

from __future__ import annotations

# ── Fixed contact info — never changes regardless of document content ─────────
FIXED_BUSINESS_PHONE = "6468459754"
FIXED_BUSINESS_EMAIL = "contracts@tvtcapital.com"

# ── Master schema — injected into AI prompt as {{JSON_SCHEMA}} ────────────────
# All string values default to "" so the AI fills only what it finds.
EXTRACTION_SCHEMA: dict[str, object] = {
    # ── Lender / ISO who submitted the application (NOT the business itself) ──
    "lander_name": "",           # ISO broker, referral partner, or funding company
    "representative_name": "",   # Individual sales rep / agent person name
    "iso_email": "",             # Email of the lender/ISO (must contain @)

    # ── Business applicant information ─────────────────────────────────────────
    "business_info": {
        "business_name": "",     # Legal business name (or owner name if sole proprietor)
        "dba_name": "",          # DBA / trade name — only if different from business_name
        "business_street": "",   # Street address only (no city/state/zip here)
        "business_city": "",
        "business_state": "",    # Always 2-letter US state code (e.g. "NY", "FL")
        "business_zip": "",      # 5-digit ZIP (e.g. "10001")
        "business_phone": FIXED_BUSINESS_PHONE,   # Fixed — do not extract from document
        "business_email": FIXED_BUSINESS_EMAIL,   # Fixed — do not extract from document
        "ein_no": "",            # 9 digits only, no hyphens (e.g. "123456789")
    },

    # ── Owner / principal information — up to 4 owners ────────────────────────
    "owners": [
        {
            "owner_name": "",    # Full name (First + Last)
            "owner_dob": "",     # Format: MM-DD-YYYY
            "owner_ssn": "",     # Format: ###-##-#### (e.g. "123-45-6789")
            "owner_street": "",  # Home street address only
            "owner_city": "",
            "owner_state": "",   # 2-letter US state code
            "owner_zip": "",     # 5-digit ZIP
        }
    ],
}

# ── Key sets used by validators.py for normalization loops ────────────────────
BUSINESS_KEYS = set(EXTRACTION_SCHEMA["business_info"])
OWNER_KEYS    = set(EXTRACTION_SCHEMA["owners"][0])
