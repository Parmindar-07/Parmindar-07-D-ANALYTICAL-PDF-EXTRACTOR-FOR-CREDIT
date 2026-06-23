"""
================================================================================
  gemini_provider.py — Google Gemini AI Provider
================================================================================

  PURPOSE:
    Sends OCR-extracted text to Google Gemini and returns the AI-generated
    JSON string containing extracted business/owner data.

  ENVIRONMENT VARIABLES:
    GEMINI_API_KEY or GOOGLE_API_KEY  — required, your Google AI API key
    GEMINI_MODEL                      — optional, defaults to "gemini-2.0-flash"

  INSTALLATION:
    pip install google-generativeai

  NOTE:
    Gemini uses response_mime_type="application/json" to enforce JSON output.
    This provider only supports text-based extraction (no direct PDF vision).
    For PDF extraction, use claude_provider.py instead.
================================================================================
"""

from __future__ import annotations

import os


# ── Text-based extraction ─────────────────────────────────────────────────────
def generate_json(prompt: str) -> str:
    """
    Send a text prompt (containing OCR output) to Gemini and return raw JSON string.

    Args:
        prompt: Full extraction prompt with OCR text already embedded.

    Returns:
        Raw JSON string from Gemini.

    Raises:
        RuntimeError: If API key is missing or google-generativeai package is not installed.
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY or GOOGLE_API_KEY is missing in .env")

    try:
        genai = __import__("google.generativeai", fromlist=[""])
    except Exception as exc:
        raise RuntimeError("google-generativeai package not installed. Run: pip install google-generativeai") from exc

    genai.configure(api_key=api_key)

    model = genai.GenerativeModel(
        os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        generation_config={
            "temperature": 0,                          # Deterministic — no hallucination
            "response_mime_type": "application/json",  # Forces Gemini to return pure JSON
        },
    )

    response = model.generate_content(prompt)
    return str(getattr(response, "text", ""))
