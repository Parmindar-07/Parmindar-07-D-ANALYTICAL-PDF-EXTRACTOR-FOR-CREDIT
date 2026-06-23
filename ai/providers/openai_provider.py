"""
================================================================================
  openai_provider.py — OpenAI GPT AI Provider
================================================================================

  PURPOSE:
    Sends OCR-extracted text to OpenAI GPT and returns the AI-generated
    JSON string containing extracted business/owner data.

  ENVIRONMENT VARIABLES:
    OPENAI_API_KEY  — required, your OpenAI API key
    OPENAI_MODEL    — optional, defaults to "gpt-4.1-mini"

  INSTALLATION:
    pip install openai

  NOTE:
    This provider only supports text-based extraction (no direct PDF vision).
    For PDF extraction, enable Claude via CLAUDE_ENABLED=true in .env.
================================================================================
"""

from __future__ import annotations

import os


# ── Text-based extraction ─────────────────────────────────────────────────────
def generate_json(prompt: str) -> str:
    """
    Send a text prompt (containing OCR output) to OpenAI and return raw JSON string.

    Args:
        prompt: Full extraction prompt with OCR text already embedded.

    Returns:
        Raw JSON string from OpenAI.

    Raises:
        RuntimeError: If API key is missing or openai package is not installed.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing in .env")

    try:
        openai = __import__("openai")
    except Exception as exc:
        raise RuntimeError("openai package not installed. Run: pip install openai") from exc

    client = openai.OpenAI(api_key=api_key)

    response = client.responses.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        input=[
            {"role": "system", "content": "Return valid JSON only."},  # No prose, pure JSON
            {"role": "user",   "content": prompt},
        ],
        temperature=0,   # Deterministic output
    )

    return str(getattr(response, "output_text", ""))
