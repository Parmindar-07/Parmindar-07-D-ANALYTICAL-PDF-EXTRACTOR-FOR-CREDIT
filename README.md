# Credit Application Data Extractor

A web-based tool that extracts structured business and owner information from business funding application PDFs and images using AI (Claude, Gemini, or OpenAI).

---

## What It Does

- Upload a PDF or image of a business funding/credit application
- Automatically extracts: Business Name, Address, EIN, Owner Names, SSNs, DOBs, and more
- Supports up to 4 business owners
- Saves results to Google Sheets (vertical column format)
- Generates a Credit Scrub report for underwriting review
- Works with text PDFs, scanned PDFs, and handwritten forms

---

## Project Structure

```
credit-app-extractor/
├── web_app.py                    # Main server + HTML UI (entry point)
├── google_sheet_sync.py          # Google Sheets integration
├── requirements.txt              # Python dependencies
├── env.example                   # Environment variable template (copy to .env)
│
├── prompts/
│   └── extraction_prompt.txt     # AI extraction rules and schema
│
├── ai/
│   ├── extractor.py              # Main AI pipeline coordinator
│   ├── providers/
│   │   ├── claude_provider.py    # Anthropic Claude (text + PDF vision)
│   │   ├── gemini_provider.py    # Google Gemini (text only)
│   │   └── openai_provider.py    # OpenAI GPT (text only)
│   ├── cleaners/
│   │   ├── text_cleaner.py       # OCR noise removal
│   │   ├── section_splitter.py   # Organize text into sections
│   │   └── validators.py         # JSON validation and context repair
│   └── schemas/
│       └── extraction_schema.py  # Output JSON schema definition
│
├── raw_text_remove_lines.txt     # Blocklist for OCR noise lines
├── all_lander_name.txt           # Known lender names for matching
└── only_for_scrub.html.html      # Scrub-only tool page
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set up environment

```bash
cp env.example .env
```

Edit `.env` and fill in:
- At least one AI provider API key
- Google Sheets credentials path and sheet IDs

### 3. Set up Google Sheets (optional)

1. Create a Google Service Account in [Google Cloud Console](https://console.cloud.google.com/)
2. Download the credentials JSON file
3. Share your Google Spreadsheet with the service account email
4. Set the credentials path in `.env`

### 4. Run

```bash
python web_app.py
```

Open your browser at: `http://127.0.0.1:8765`

---

## AI Provider Setup

Enable at least one provider in `.env`:

| Provider | Environment Variable | Model Default |
|----------|----------------------|---------------|
| Claude   | `CLAUDE_ENABLED=true` + `CLAUDE_API_KEY=` | `claude-haiku-4-5-20251001` |
| Gemini   | `GEMINI_ENABLED=true` + `GEMINI_API_KEY=` | `gemini-2.0-flash` |
| OpenAI   | `OPENAI_ENABLED=true` + `OPENAI_API_KEY=` | `gpt-4.1-mini` |

> **Note:** Only Claude supports direct PDF extraction (no OCR needed). Recommended for handwritten or scanned forms.

---

## How It Works

### PDF Upload Flow
```
PDF Upload → Claude AI (reads PDF directly) → Structured JSON → Form Fields + Google Sheet
```

### Image / Handwritten PDF Flow
```
Image Upload → OCR Pipeline → Raw Text → AI Extraction → Structured JSON → Form Fields + Google Sheet
```

### AI Extraction Pipeline
```
Raw OCR Text
    → text_cleaner.py    (remove noise)
    → section_splitter.py (organize into sections)
    → extractor.py        (build prompt + call AI)
    → validators.py       (normalize + context repair)
    → Frontend form fill
```

---

## Optional Dependencies

For image OCR and handwriting support:

```bash
pip install paddleocr paddlepaddle opencv-python
```

Uncomment these in `requirements.txt` if needed.

---

## Environment Variables Reference

| Variable | Description | Default |
|----------|-------------|---------|
| `OCR_WEB_PORT` | Server port | `8765` |
| `AI_PROVIDER_ORDER` | Provider priority | `gemini,claude,openai` |
| `AI_TEXT_LIMIT` | Max chars sent to AI | `8000` |
| `RAW_TEXT_MAX_LINE_LENGTH` | Max line length for AI input | `70` |
| `CREDIT_GOOGLE_SHEET_ENABLED` | Enable/disable sheet sync | `true` |
| `CREDIT_GOOGLE_CREDENTIALS_PATH` | Path to service account JSON | — |
| `GOOGLE_SHEET_ID_1` through `_9` | Spreadsheet IDs | — |

---

## Google Sheets Format

Data is written **vertically** (not in rows):
- **Column A** = Field labels (frozen, written once)
- **Column B+** = One submission per column

If a submission has 3-4 owners, two columns are written (Owners 1&2, then Owners 3&4).

---

## Security Notes

- Never commit `.env` to version control — it contains API keys
- `credentials.json` is in `.gitignore` — keep it outside the project root
- The server binds to `127.0.0.1` only (not accessible from the network)
- Sheet clear requires a password (`2580`) as a basic safeguard

---

## License

MIT
