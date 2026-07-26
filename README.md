# agentic-chatbot

Converts meeting notes into tasks, assigns owners, adds deadlines, updates project tools, and follows up on overdue items.

## Phase 1: offline AMI preprocessing

The first implemented component creates controlled test data from AMI manual annotations. It is offline-only: it does not call an LLM, expose an MCP server, or access email, calendar, or project tools.

Generate review artifacts for `ES2002a`:

```bash
PYTHONPATH=src python3 -m meeting_preprocessor.cli \
  --source-dir "/Users/audrey/Desktop/Agentic project/ami_public_manual_1.6.2" \
  --meeting-id ES2002a \
  --output-dir data/processed/ES2002a
```

The command creates a canonical JSON transcript, raw parsed JSON, TXT, Markdown, CSV, DOCX, PDF, source manifest, and conversion report. Generated artifacts live under `data/processed/` and are intentionally ignored by Git.

Run the test suite:

```bash
PYTHONPATH=src python3 -m unittest discover -v
```

## Phase 2: production converter MCP

The converter MCP accepts only paths relative to `CONVERTER_UPLOAD_ROOT` (default: `data/uploads/`). It detects and converts AMI directories/XML, canonical JSON, TXT, Markdown, CSV, DOCX, and text PDFs to the same canonical JSON contract. Results are saved below `CONVERTER_ARTIFACT_ROOT` (default: `data/conversions/`).

Run it over stdio for an MCP client:

```bash
PYTHONPATH=src python3 -m meeting_preprocessor.server
```

It exposes `detect_document_type`, `convert_document`, `validate_transcript`, `render_transcript`, and `get_conversion_report`. The server has no email, calendar, project-management, or reminder tools.
