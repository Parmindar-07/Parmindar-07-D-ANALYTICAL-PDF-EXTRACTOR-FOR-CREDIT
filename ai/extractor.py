"""
================================================================================
  extractor.py — Main AI Extraction Pipeline Coordinator
================================================================================

  PURPOSE:
    Central module that ties together all AI providers, prompt building,
    text cleaning, and JSON extraction. Called by web_app.py for all
    AI-related operations.

  MAIN FUNCTIONS:
    extract_ai_json(raw_ocr_text)         — Text → AI → validated JSON dict
    extract_ai_json_from_pdf(pdf_bytes)   — PDF bytes → Claude AI → validated JSON dict
    ai_json_to_frontend_values(data)      — JSON dict → flat frontend key-value dict
    extract_frontend_values(raw_ocr_text) — Convenience: text → (raw_data, frontend_values)

  PROVIDER SELECTION:
    Reads .env to find which providers are enabled and in what order.
    Order is set via AI_PROVIDER_ORDER=gemini,claude,openai
    The first enabled provider in that order is used.

  PROMPT BUILDING:
    The prompt file (prompts/extraction_prompt.txt) contains two placeholders:
      {{JSON_SCHEMA}}  → replaced with EXTRACTION_SCHEMA as JSON
      {{RAW_TEXT}}     → replaced with cleaned OCR text (capped at AI_TEXT_LIMIT chars)

  ENVIRONMENT VARIABLES:
    AI_PROVIDER_ORDER    — comma-separated provider priority (default: gemini,claude,openai)
    AI_PROMPT_PATH       — path to prompt file (default: prompts/extraction_prompt.txt)
    AI_TEXT_LIMIT        — max characters sent to AI (default: 12000)
    GEMINI_ENABLED       — true/false
    CLAUDE_ENABLED       — true/false (also ANTHROPIC_ENABLED)
    OPENAI_ENABLED       — true/false
================================================================================
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ai.schemas.extraction_schema import EXTRACTION_SCHEMA


# ── Provider module registry ───────────────────────────────────────────────────
# Maps provider name → importable module path
PROVIDERS = {
    "gemini":    "ai.providers.gemini_provider",
    "claude":    "ai.providers.claude_provider",
    "anthropic": "ai.providers.claude_provider",   # alias for claude
    "openai":    "ai.providers.openai_provider",
}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Environment Setup
# ══════════════════════════════════════════════════════════════════════════════

def load_env_file(path: str = ".env") -> None:
    """
    Manually parse a .env file and inject variables into os.environ.
    Skips variables that are already set (does not override system env).
    Strips surrounding quotes from values.
    """
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key   = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# Load .env immediately when this module is imported
load_env_file()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Prompt Building
# ══════════════════════════════════════════════════════════════════════════════

def load_prompt_file() -> str:
    """
    Read the extraction prompt template from disk.
    Path is configured via AI_PROMPT_PATH in .env (default: prompts/extraction_prompt.txt).

    Raises:
        RuntimeError: If the prompt file does not exist.
    """
    path = Path(os.getenv("AI_PROMPT_PATH", "prompts/extraction_prompt.txt"))
    if not path.exists():
        raise RuntimeError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def build_prompt(raw_ocr_text: str) -> str:
    """
    Build the full AI prompt by injecting schema and OCR text into the template.

    Template placeholders:
      {{JSON_SCHEMA}} → EXTRACTION_SCHEMA serialized as pretty JSON
      {{RAW_TEXT}}    → Cleaned OCR text, capped at AI_TEXT_LIMIT characters

    Args:
        raw_ocr_text: Cleaned OCR output from text_cleaner.clean_ocr_text().

    Returns:
        Complete prompt string ready to send to an AI provider.
    """
    raw_text = str(raw_ocr_text or "")
    text_limit = int(os.getenv("AI_TEXT_LIMIT", "12000"))
    return (
        load_prompt_file()
        .replace("{{JSON_SCHEMA}}", json.dumps(EXTRACTION_SCHEMA, indent=2))
        .replace("{{RAW_TEXT}}",   raw_text[:text_limit])
    )


def build_pdf_prompt() -> str:
    """
    Build the prompt for direct PDF extraction (no OCR text involved).
    Removes the "RAW OCR TEXT:" section from the template since Claude
    reads the PDF directly — the text is not needed in the prompt.

    Returns:
        Prompt string without the {{RAW_TEXT}} section.
    """
    prompt = load_prompt_file()
    prompt = prompt.replace("{{JSON_SCHEMA}}", json.dumps(EXTRACTION_SCHEMA, indent=2))
    # Remove the RAW OCR TEXT block — Claude reads the PDF directly
    prompt = re.sub(
        r"\nRAW OCR TEXT:\s*\{\{RAW_TEXT\}\}.*",
        "\nExtract all required data from the attached PDF document.",
        prompt,
        flags=re.S,
    )
    return prompt


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Provider Selection
# ══════════════════════════════════════════════════════════════════════════════

def selected_provider() -> str:
    """
    Determine which AI provider to use based on .env configuration.

    Reads GEMINI_ENABLED, CLAUDE_ENABLED (or ANTHROPIC_ENABLED), OPENAI_ENABLED.
    Picks the first enabled provider in the order defined by AI_PROVIDER_ORDER.

    Returns:
        Provider name string: "gemini", "claude", or "openai".

    Raises:
        RuntimeError: If no provider is enabled.
    """
    enabled_flags = {
        "gemini": os.getenv("GEMINI_ENABLED", "false"),
        "claude": os.getenv("CLAUDE_ENABLED", os.getenv("ANTHROPIC_ENABLED", "false")),
        "openai": os.getenv("OPENAI_ENABLED", "false"),
    }
    enabled = {
        provider
        for provider, value in enabled_flags.items()
        if value.strip().lower() in {"1", "true", "yes", "on"}
    }
    if not enabled:
        raise RuntimeError(
            "No AI provider enabled. Set one of:\n"
            "  GEMINI_ENABLED=true\n"
            "  CLAUDE_ENABLED=true\n"
            "  OPENAI_ENABLED=true\n"
            "in your .env file."
        )
    # Respect the user's priority order
    order = [
        item.strip().lower()
        for item in os.getenv("AI_PROVIDER_ORDER", "gemini,claude,openai").split(",")
    ]
    for provider in order:
        if provider in enabled:
            return provider
    # Fallback: alphabetically first enabled provider
    return sorted(enabled)[0]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Provider Module Loading & JSON Parsing
# ══════════════════════════════════════════════════════════════════════════════

def _provider_module(provider: str):
    """
    Dynamically import and return the provider module.

    Args:
        provider: One of "gemini", "claude", "anthropic", "openai".

    Raises:
        RuntimeError: If the provider name is not recognized.
    """
    if provider not in PROVIDERS:
        raise RuntimeError(
            f"Unsupported AI provider '{provider}'. Use: gemini, claude, or openai."
        )
    module_name = PROVIDERS[provider]
    components  = module_name.split(".")
    return __import__(module_name, fromlist=[components[-1]])


def _extract_json_object(text: str) -> dict[str, object]:
    """
    Parse a raw AI response string into a Python dict.

    Handles:
      - Pure JSON strings
      - JSON wrapped in ```json ... ``` markdown fences
      - JSON buried somewhere in a longer text response

    Raises:
        json.JSONDecodeError: If no valid JSON object is found.
        RuntimeError: If the parsed value is not a dict.
    """
    cleaned = str(text or "").strip()

    # Remove markdown code fences if present
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I | re.S).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to extract just the JSON object portion
        start = cleaned.find("{")
        end   = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(cleaned[start:end + 1])

    if not isinstance(data, dict):
        raise RuntimeError("AI response was not a JSON object.")
    return data


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Main Extraction Functions
# ══════════════════════════════════════════════════════════════════════════════

def extract_ai_json_from_pdf(pdf_bytes: bytes, progress=None) -> dict[str, object]:
    """
    Extract structured data directly from a PDF using Claude's document API.
    No OCR is involved — Claude reads the PDF natively (handles handwriting better).

    Only Claude supports this mode. If Claude is not enabled, this will raise.

    Args:
        pdf_bytes: Raw bytes of the uploaded PDF file.
        progress:  Optional callable(str) for status updates.

    Returns:
        Validated dict with "business_info" and "owners" keys.

    Raises:
        RuntimeError: If selected provider does not support PDF extraction,
                      or if API call fails.
    """
    provider = selected_provider()
    module   = _provider_module(provider)

    if not hasattr(module, "generate_json_from_pdf"):
        raise RuntimeError(
            f"Provider '{provider}' does not support direct PDF extraction.\n"
            "Enable Claude: set CLAUDE_ENABLED=true in .env"
        )

    if progress:
        progress("Sending PDF to AI...")

    prompt       = build_pdf_prompt()
    raw_response = module.generate_json_from_pdf(pdf_bytes, prompt)
    data         = _extract_json_object(raw_response)

    if progress:
        progress("Done")

    return data


def extract_ai_json(raw_ocr_text: str, progress=None) -> dict[str, object]:
    """
    Extract structured data from OCR text using the configured AI provider.

    Flow:
      1. Select active provider
      2. Build prompt with schema + OCR text
      3. Call provider's generate_json()
      4. Parse JSON response
      5. Return raw dict (validation is done in web_app.py via validators.py)

    Args:
        raw_ocr_text: Cleaned OCR text (output of text_cleaner.clean_ocr_text()).
        progress:     Optional callable(str) for status updates.

    Returns:
        Raw dict from AI (not yet validated — call validators.validate_ai_json() next).
    """
    provider = selected_provider()

    if progress:
        progress("Processing...")

    module       = _provider_module(provider)
    prompt       = build_prompt(raw_ocr_text)
    raw_response = module.generate_json(prompt)
    data         = _extract_json_object(raw_response)

    if progress:
        progress("Done")

    return data


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — Frontend Value Mapping
# ══════════════════════════════════════════════════════════════════════════════

def _plain_text(value: object) -> str:
    """Convert any value to string, treating None as empty string."""
    return "" if value is None else str(value)


def ai_json_to_frontend_values(data: dict[str, object]) -> dict[str, str]:
    """
    Flatten nested AI JSON into the flat key-value dict used by the frontend form.

    The frontend form fields use camelCase HTML IDs (businessName, ownerDob, etc.)
    which differ from the AI schema's snake_case keys (business_name, owner_dob).

    Owner naming convention:
      Owner 1 → prefix "owner"     (ownerName, ownerDob, ownerAddress, ..., ssn)
      Owner 2 → prefix "owner2"    (owner2Name, owner2Dob, ..., owner2Ssn)
      Owner 3 → prefix "owner3"    ...
      Owner 4 → prefix "owner4"    ...

    Args:
        data: Validated AI JSON dict (output of validators.validate_ai_json()).

    Returns:
        Flat dict of frontend field key → value strings.
    """
    business = data.get("business_info") if isinstance(data, dict) else {}
    business = business if isinstance(business, dict) else {}

    values: dict[str, str] = {
        # ── Business fields ────────────────────────────────────────────────
        "businessName":    _plain_text(business.get("business_name", "")),
        "businessDbaName": _plain_text(business.get("dba_name", "")),
        "businessAddress": _plain_text(business.get("business_street", "")),
        "businessCity":    _plain_text(business.get("business_city", "")),
        "businessState":   _plain_text(business.get("business_state", "")),
        "businessZip":     _plain_text(business.get("business_zip", "")),
        "businessPhone":   _plain_text(business.get("business_phone", "")),
        "businessEmail":   _plain_text(business.get("business_email", "")),
        "einNumber":       _plain_text(business.get("ein_no", "")),
        # ── Lender / ISO fields ────────────────────────────────────────────
        "lander_name":        _plain_text(data.get("lander_name", "")        if isinstance(data, dict) else ""),
        "representative_name": _plain_text(data.get("representative_name", "") if isinstance(data, dict) else ""),
        "iso_email":          _plain_text(data.get("iso_email", "")           if isinstance(data, dict) else ""),
    }

    # ── Owner fields (up to 4 owners) ─────────────────────────────────────────
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


def extract_frontend_values(
    raw_ocr_text: str,
    progress=None,
) -> tuple[dict[str, object], dict[str, str]]:
    """
    Convenience function: runs extraction and mapping in one call.

    Returns:
        (raw_ai_data, frontend_values_dict)
    """
    data = extract_ai_json(raw_ocr_text, progress)
    return data, ai_json_to_frontend_values(data)
