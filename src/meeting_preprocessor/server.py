"""MCP entry point for the production transcript converter."""

from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .converter import InputValidationError, TranscriptConverter


def _converter() -> TranscriptConverter:
    upload_root = Path(os.getenv("CONVERTER_UPLOAD_ROOT", "data/uploads"))
    artifact_root = Path(os.getenv("CONVERTER_ARTIFACT_ROOT", "data/conversions"))
    max_size = int(os.getenv("MAX_UPLOAD_SIZE_MB", "25")) * 1024 * 1024
    return TranscriptConverter(upload_root, artifact_root, max_size)


mcp = FastMCP(
    "Transcript Converter",
    instructions=(
        "Convert only application-controlled uploads into canonical transcript JSON. "
        "Do not use this server for email, calendar, task, or reminder actions."
    ),
    json_response=True,
)


@mcp.tool()
def detect_document_type(artifact_path: str) -> str:
    """Inspect a relative upload path and report its supported document type."""
    return json.dumps(_converter().detect_document_type(artifact_path))


@mcp.tool()
def convert_document(artifact_path: str, meeting_id: str | None = None) -> str:
    """Convert a controlled upload into canonical transcript JSON and persist the result."""
    try:
        return json.dumps(_converter().convert_document(artifact_path, meeting_id).as_dict(), ensure_ascii=False)
    except (InputValidationError, ValueError) as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def validate_transcript(artifact_path: str) -> str:
    """Validate whether an uploaded JSON file already follows the canonical transcript schema."""
    return json.dumps(_converter().validate_json_artifact(artifact_path))


@mcp.tool()
def render_transcript(conversion_id: str, output_format: str) -> str:
    """Render a stored conversion as TXT, Markdown, or CSV."""
    return json.dumps(_converter().render_converted_transcript(conversion_id, output_format))


@mcp.tool()
def get_conversion_report(conversion_id: str) -> str:
    """Return warnings and counts for a stored conversion."""
    return json.dumps(_converter().conversion_report(conversion_id))


def main() -> None:
    transport = os.getenv("CONVERTER_MCP_TRANSPORT", "stdio")
    if transport not in {"stdio", "streamable-http"}:
        raise ValueError("CONVERTER_MCP_TRANSPORT must be stdio or streamable-http")
    if transport == "streamable-http":
        mcp.settings.host = os.getenv("CONVERTER_MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.getenv("CONVERTER_MCP_PORT", "8090"))
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
