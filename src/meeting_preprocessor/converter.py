"""Production-safe conversion service behind the transcript converter MCP."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4
import xml.etree.ElementTree as ET
import zipfile

from pypdf import PdfReader

from .ami import ConversionError, PARSER_VERSION, SCHEMA_VERSION, convert_ami_meeting, validate_transcript
from .renderers import render_csv, render_markdown, render_text


SUPPORTED_EXTENSIONS = {".csv", ".docx", ".json", ".md", ".pdf", ".txt", ".xml"}
TRANSCRIPT_LINE = re.compile(
    r"^\[(?P<start>\d{2}:\d{2}:\d{2}(?:\.\d{3})?)\s*[–-]\s*"
    r"(?P<end>\d{2}:\d{2}:\d{2}(?:\.\d{3})?)\]\s*"
    r"(?:Speaker\s+)?(?P<speaker>[^:]+):\s*(?P<text>.+)$"
)
SPEAKER_LINE = re.compile(r"^(?:Speaker\s+)?(?P<speaker>[A-Za-z][\w .'-]{0,60}):\s*(?P<text>.+)$")


class InputValidationError(ValueError):
    """Raised when an MCP caller supplies an unsafe or unsupported artifact."""


def _seconds(value: str) -> float:
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass(frozen=True)
class ConversionResult:
    conversion_id: str
    transcript: dict[str, Any]
    report: dict[str, Any]
    artifact_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": SCHEMA_VERSION,
            "conversion_id": self.conversion_id,
            "status": "completed_with_warnings" if self.report["warnings"] else "completed",
            "transcript": self.transcript,
            "report": self.report,
        }
        if self.artifact_path:
            result["artifact"] = {"format": "transcript+json", "path": self.artifact_path}
        return result


class TranscriptConverter:
    """Converts application-controlled upload artifacts to canonical transcript JSON."""

    def __init__(self, upload_root: Path, artifact_root: Path, max_upload_size_bytes: int = 25 * 1024 * 1024):
        self.upload_root = upload_root.resolve()
        self.artifact_root = artifact_root.resolve()
        self.max_upload_size_bytes = max_upload_size_bytes

    def _resolve_upload(self, relative_path: str) -> Path:
        candidate = (self.upload_root / relative_path).resolve()
        try:
            candidate.relative_to(self.upload_root)
        except ValueError as exc:
            raise InputValidationError("artifact_path must remain inside the configured upload root") from exc
        if not candidate.exists():
            raise InputValidationError(f"Uploaded artifact does not exist: {relative_path}")
        if candidate.is_file() and candidate.stat().st_size > self.max_upload_size_bytes:
            raise InputValidationError("Uploaded artifact exceeds the configured size limit")
        if candidate.is_dir():
            total_bytes = sum(file_path.stat().st_size for file_path in candidate.rglob("*") if file_path.is_file())
            if total_bytes > self.max_upload_size_bytes:
                raise InputValidationError("Uploaded artifact directory exceeds the configured size limit")
        return candidate

    def detect_document_type(self, artifact_path: str) -> dict[str, Any]:
        path = self._resolve_upload(artifact_path)
        if path.is_dir() and (path / "words").is_dir() and (path / "segments").is_dir():
            return {"artifact_path": artifact_path, "document_type": "ami_directory", "supported": True}
        if not path.is_file():
            return {"artifact_path": artifact_path, "document_type": "directory", "supported": False}
        header = path.read_bytes()[:8]
        extension = path.suffix.lower()
        if header.startswith(b"%PDF-"):
            kind = "pdf"
        elif header.startswith(b"PK\x03\x04") and extension == ".docx":
            kind = "docx"
        elif extension == ".xml" and b"nite" in path.read_bytes()[:4096].lower():
            kind = "ami_xml"
        elif extension == ".json":
            kind = "json"
        elif extension == ".csv":
            kind = "csv"
        elif extension == ".md":
            kind = "markdown"
        elif extension == ".txt":
            kind = "text"
        else:
            kind = "unknown"
        return {
            "artifact_path": artifact_path,
            "document_type": kind,
            "supported": kind != "unknown",
            "bytes": path.stat().st_size,
        }

    def convert_document(self, artifact_path: str, meeting_id: str | None = None) -> ConversionResult:
        path = self._resolve_upload(artifact_path)
        detection = self.detect_document_type(artifact_path)
        kind = detection["document_type"]
        if kind == "ami_directory":
            if not meeting_id:
                raise InputValidationError("meeting_id is required when converting an AMI directory")
            transcript, _, metadata = convert_ami_meeting(path, meeting_id)
            report = metadata["report"]
        elif kind == "ami_xml":
            source_dir = path.parent.parent if path.parent.name in {"words", "segments"} else None
            if source_dir is None or not meeting_id:
                raise InputValidationError("AMI XML conversion requires a words/ or segments/ file and meeting_id")
            transcript, _, metadata = convert_ami_meeting(source_dir, meeting_id)
            report = metadata["report"]
        elif kind == "json":
            transcript = self._convert_json(path)
            report = self._report(transcript, "json_schema_validation", [])
        elif kind in {"text", "markdown", "docx", "pdf", "csv"}:
            transcript, warnings = self._convert_textual(path, kind, meeting_id)
            transcript["source"]["files"] = [artifact_path]
            report = self._report(transcript, f"deterministic_{kind}", warnings)
        else:
            raise InputValidationError(f"Unsupported document type for {artifact_path}")
        transcript["conversion"]["warnings"] = report["warnings"]
        errors = validate_transcript(transcript)
        if errors:
            raise ConversionError("Canonical transcript validation failed: " + "; ".join(errors))
        conversion_id = self._conversion_id(path, meeting_id)
        artifact_path_out = self._write_artifact(conversion_id, transcript, report)
        return ConversionResult(conversion_id, transcript, report, artifact_path_out)

    def validate_json_artifact(self, artifact_path: str) -> dict[str, Any]:
        path = self._resolve_upload(artifact_path)
        if path.suffix.lower() != ".json":
            raise InputValidationError("validate_transcript accepts only JSON upload artifacts")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"valid": False, "errors": [str(exc)]}
        return {"valid": not (errors := validate_transcript(payload)), "errors": errors}

    def render_converted_transcript(self, conversion_id: str, output_format: str) -> dict[str, Any]:
        if output_format not in {"txt", "md", "csv"}:
            raise InputValidationError("output_format must be one of: txt, md, csv")
        path = (self.artifact_root / conversion_id / "transcript.cleaned.json").resolve()
        try:
            path.relative_to(self.artifact_root)
        except ValueError as exc:
            raise InputValidationError("Invalid conversion ID") from exc
        if not path.is_file():
            raise InputValidationError(f"Unknown conversion ID: {conversion_id}")
        transcript = json.loads(path.read_text(encoding="utf-8"))
        rendered = {"txt": render_text, "md": render_markdown, "csv": render_csv}[output_format](transcript)
        target = path.parent / f"transcript.{output_format}"
        target.write_text(rendered, encoding="utf-8")
        return {"conversion_id": conversion_id, "format": output_format, "artifact_path": str(target.relative_to(self.artifact_root))}

    def conversion_report(self, conversion_id: str) -> dict[str, Any]:
        path = self.artifact_root / conversion_id / "conversion_report.json"
        if not path.is_file():
            raise InputValidationError(f"Unknown conversion ID: {conversion_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _convert_json(self, path: Path) -> dict[str, Any]:
        try:
            transcript = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InputValidationError(f"Invalid JSON transcript: {exc}") from exc
        errors = validate_transcript(transcript)
        if errors:
            raise InputValidationError("JSON is not canonical transcript JSON: " + "; ".join(errors))
        return transcript

    def _convert_textual(self, path: Path, kind: str, meeting_id: str | None) -> tuple[dict[str, Any], list[str]]:
        if kind == "docx":
            paragraphs = self._read_docx(path)
        elif kind == "pdf":
            paragraphs = self._read_pdf(path)
        elif kind == "csv":
            return self._read_csv(path, meeting_id)
        else:
            try:
                paragraphs = path.read_text(encoding="utf-8-sig").splitlines()
            except UnicodeDecodeError as exc:
                raise InputValidationError("Text upload must be UTF-8 encoded") from exc
        return self._turns_from_lines(paragraphs, path, meeting_id, kind)

    def _read_docx(self, path: Path) -> list[str]:
        try:
            with zipfile.ZipFile(path) as archive:
                root = ET.fromstring(archive.read("word/document.xml"))
        except (KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
            raise InputValidationError("Invalid DOCX document") from exc
        paragraphs = []
        for paragraph in root.iter():
            if _local_name(paragraph.tag) != "p":
                continue
            text = "".join((element.text or "") for element in paragraph.iter() if _local_name(element.tag) == "t").strip()
            if text:
                paragraphs.append(text)
        return paragraphs

    def _read_pdf(self, path: Path) -> list[str]:
        try:
            reader = PdfReader(str(path))
            return [line.strip() for page in reader.pages for line in (page.extract_text() or "").splitlines() if line.strip()]
        except Exception as exc:  # pypdf may raise several parser-specific exceptions.
            raise InputValidationError("Unable to extract text from PDF; scanned PDFs require the later OCR pipeline") from exc

    def _read_csv(self, path: Path, meeting_id: str | None) -> tuple[dict[str, Any], list[str]]:
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
        except UnicodeDecodeError as exc:
            raise InputValidationError("CSV upload must be UTF-8 encoded") from exc
        if not rows:
            raise InputValidationError("CSV contains no transcript rows")
        turns = []
        warnings = []
        for index, row in enumerate(rows, start=1):
            text = row.get("text") or row.get("transcript") or row.get("content")
            if not text:
                warnings.append(f"CSV row {index} has no text and was skipped.")
                continue
            start = self._number_or_none(row.get("start"))
            end = self._number_or_none(row.get("end"))
            turns.append(self._turn(index, row.get("speaker_id") or row.get("speaker") or "unknown", start, end, text, {"row": index}))
        return self._transcript(meeting_id or path.stem, "csv", turns), warnings

    @staticmethod
    def _number_or_none(value: str | None) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except ValueError as exc:
            raise InputValidationError(f"Invalid timestamp {value!r} in CSV") from exc

    def _turns_from_lines(self, lines: list[str], path: Path, meeting_id: str | None, input_format: str) -> tuple[dict[str, Any], list[str]]:
        turns = []
        warnings = []
        for index, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line or (input_format == "markdown" and line.startswith("#")) or line.lower().startswith("meeting "):
                continue
            timed = TRANSCRIPT_LINE.match(line)
            spoken = SPEAKER_LINE.match(line)
            if timed:
                turns.append(self._turn(index, timed.group("speaker"), _seconds(timed.group("start")), _seconds(timed.group("end")), timed.group("text"), {"line": index}))
            elif spoken:
                turns.append(self._turn(index, spoken.group("speaker"), None, None, spoken.group("text"), {"line": index}))
            else:
                turns.append(self._turn(index, "unknown", None, None, line, {"line": index}))
                warnings.append(f"Line {index} has no recognized speaker or timestamp.")
        if not turns:
            raise InputValidationError("Document contains no usable transcript text")
        return self._transcript(meeting_id or path.stem, input_format, turns), warnings

    @staticmethod
    def _turn(index: int, speaker_id: str, start: float | None, end: float | None, text: str, location: dict[str, int]) -> dict[str, Any]:
        return {
            "turn_id": f"turn-{index:04d}",
            "speaker_id": speaker_id.strip() or "unknown",
            "start": start,
            "end": end,
            "text": text.strip(),
            "provenance": {"segment_id": None, "page": location.get("page"), "paragraph": location.get("line"), "source_element_ids": []},
        }

    def _transcript(self, meeting_id: str, input_format: str, turns: list[dict[str, Any]]) -> dict[str, Any]:
        speakers = []
        for turn in turns:
            if turn["speaker_id"] not in speakers:
                speakers.append(turn["speaker_id"])
        return {
            "schema_version": SCHEMA_VERSION,
            "meeting_id": meeting_id,
            "meeting_date": None,
            "timezone": None,
            "language": "en",
            "source": {"dataset": None, "input_format": input_format, "files": []},
            "participants": [{"speaker_id": speaker, "display_name": None} for speaker in speakers],
            "turns": turns,
            "conversion": {"method": f"deterministic_{input_format}", "parser_version": PARSER_VERSION, "llm_normalization_used": False, "warnings": []},
        }

    @staticmethod
    def _report(transcript: dict[str, Any], method: str, warnings: list[str]) -> dict[str, Any]:
        return {
            "meeting_id": transcript["meeting_id"],
            "parser_version": PARSER_VERSION,
            "method": method,
            "turn_count": len(transcript["turns"]),
            "warnings": warnings,
        }

    @staticmethod
    def _conversion_id(path: Path, meeting_id: str | None) -> str:
        if path.is_file():
            contents = path.read_bytes()
        else:
            digest_input = bytearray()
            for file_path in sorted(child for child in path.rglob("*") if child.is_file()):
                digest_input.extend(str(file_path.relative_to(path)).encode("utf-8"))
                digest_input.extend(file_path.read_bytes())
            contents = bytes(digest_input)
        digest = sha256(contents).hexdigest()[:16]
        return f"conv_{digest}_{meeting_id or 'document'}"

    def _write_artifact(self, conversion_id: str, transcript: dict[str, Any], report: dict[str, Any]) -> str:
        target = self.artifact_root / conversion_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "transcript.cleaned.json").write_text(json.dumps(transcript, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        (target / "conversion_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        return str((target / "transcript.cleaned.json").relative_to(self.artifact_root))
