"""
================================================================================
  claude_provider.py — Anthropic Claude AI Provider
================================================================================

  PURPOSE:
    Sends OCR text or raw PDF bytes to Anthropic Claude and returns the
    AI-generated JSON string containing extracted business/owner data.

  TWO MODES:
    1. generate_json(prompt)          — text-based extraction (OCR → AI)
    2. generate_json_from_pdf(bytes)  — direct PDF vision extraction (PDF → AI)

  ENVIRONMENT VARIABLES:
    CLAUDE_API_KEY or ANTHROPIC_API_KEY   — required, your Anthropic API key
    CLAUDE_MODEL or ANTHROPIC_MODEL       — optional, defaults to claude-haiku-4-5-20251001
    CLAUDE_MAX_TOKENS or ANTHROPIC_MAX_TOKENS — optional, defaults to 8000

  INSTALLATION:
    pip install anthropic

  NOTE:
    Only Claude supports direct PDF extraction via the document API.
    Gemini and OpenAI providers only support text-based extraction.
================================================================================
"""

from __future__ import annotations

import base64
import os


# ── Text-based extraction ─────────────────────────────────────────────────────
def generate_json(prompt: str) -> str:
    """
    Send a text prompt (containing OCR output) to Claude and return raw JSON string.

    Args:
        prompt: Full extraction prompt with OCR text already embedded.

    Returns:
        Raw JSON string from Claude (may include markdown fences — caller strips them).

    Raises:
        RuntimeError: If API key is missing or anthropic package is not installed.
    """
    api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("CLAUDE_API_KEY or ANTHROPIC_API_KEY is missing in .env")

    try:
        anthropic = __import__("anthropic")
    except Exception as exc:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic") from exc

    client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model=os.getenv("CLAUDE_MODEL") or os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=int(os.getenv("CLAUDE_MAX_TOKENS", os.getenv("ANTHROPIC_MAX_TOKENS", "8000"))),
        temperature=0,                          # Deterministic output — no creativity needed
        system="Return valid JSON only.",       # Forces pure JSON response, no prose
        messages=[{"role": "user", "content": prompt}],
    )

    # Concatenate all text blocks from Claude's response
    return "\n".join(
        str(getattr(block, "text", ""))
        for block in getattr(response, "content", []) or []
    )


# ── Direct PDF extraction (vision API) ───────────────────────────────────────
def generate_json_from_pdf(pdf_bytes: bytes, prompt: str) -> str:
    """
    Send a PDF file directly to Claude's document API for extraction.
    This bypasses OCR entirely — Claude reads the PDF natively.

    Args:
        pdf_bytes: Raw bytes of the uploaded PDF file.
        prompt:    Extraction instructions (without {{RAW_TEXT}} section).

    Returns:
        Raw JSON string from Claude.

    Raises:
        RuntimeError: If API key is missing or anthropic package is not installed.
    """
    api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("CLAUDE_API_KEY or ANTHROPIC_API_KEY is missing in .env")

    try:
        anthropic = __import__("anthropic")
    except Exception as exc:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic") from exc

    client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model=os.getenv("CLAUDE_MODEL") or os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=int(os.getenv("CLAUDE_MAX_TOKENS", os.getenv("ANTHROPIC_MAX_TOKENS", "8000"))),
        temperature=0,
        system="Return valid JSON only.",
        messages=[{
            "role": "user",
            "content": [
                {
                    # The PDF is sent as a base64-encoded document block
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.b64encode(pdf_bytes).decode("utf-8"),
                    },
                },
                {
                    # Extraction instructions follow the document
                    "type": "text",
                    "text": prompt,
                },
            ],
        }],
    )

    return "\n".join(
        str(getattr(block, "text", ""))
        for block in getattr(response, "content", []) or []
    )
