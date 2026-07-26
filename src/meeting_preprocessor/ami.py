"""Convert AMI manual word and segment annotations into transcript artifacts."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import json
import re
from typing import Any
import xml.etree.ElementTree as ET

from .renderers import render_csv, render_docx, render_markdown, render_pdf, render_text


SCHEMA_VERSION = "1.0"
PARSER_VERSION = "0.1.0"
NITE_ID = "{http://nite.sourceforge.net/}id"
REFERENCE_PATTERN = re.compile(r"#id\(([^)]+)\)(?:\.\.id\(([^)]+)\))?$")
FILE_PATTERN = re.compile(r"^(?P<meeting>.+)\.(?P<speaker>[^.]+)\.(?P<kind>words|segments)\.xml$")


class ConversionError(ValueError):
    """Raised when AMI input cannot be converted without losing provenance."""


@dataclass(frozen=True)
class Token:
    source_id: str
    kind: str
    text: str | None
    start: float | None
    end: float | None
    punctuation: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.kind,
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "punctuation": self.punctuation,
        }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _nite_id(element: ET.Element) -> str:
    value = element.get(NITE_ID) or element.get("nite:id")
    if not value:
        raise ConversionError(f"Missing nite:id on <{_local_name(element.tag)}> element")
    return value


def _float_attribute(element: ET.Element, name: str) -> float | None:
    value = element.get(name)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ConversionError(f"Invalid {name}={value!r} on {_nite_id(element)}") from exc


def _token_from_element(element: ET.Element) -> Token | None:
    kind = _local_name(element.tag)
    if kind not in {"w", "vocalsound", "gap", "disfmarker"}:
        return None
    source_id = _nite_id(element)
    start = _float_attribute(element, "starttime")
    end = _float_attribute(element, "endtime")
    if kind == "w":
        return Token(
            source_id=source_id,
            kind="word",
            text=(element.text or "").strip(),
            start=start,
            end=end,
            punctuation=element.get("punc") == "true",
        )
    if kind == "vocalsound":
        return Token(source_id, "vocal_sound", f"[{element.get('type', 'sound')}]", start, end)
    if kind == "gap":
        return Token(source_id, "gap", "[gap]", start, end)
    # Disfluency markers identify an annotation span but contain no speakable text.
    return Token(source_id, "disfluency_marker", None, start, end)


def _parse_words(path: Path) -> tuple[list[Token], dict[str, int]]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ConversionError(f"Invalid XML in {path}") from exc
    tokens: list[Token] = []
    positions: dict[str, int] = {}
    for element in root:
        token = _token_from_element(element)
        if token is None:
            continue
        if token.source_id in positions:
            raise ConversionError(f"Duplicate word ID {token.source_id} in {path}")
        positions[token.source_id] = len(tokens)
        tokens.append(token)
    return tokens, positions


def _reference_ids(reference: str, tokens: list[Token], positions: dict[str, int], source: Path) -> list[str]:
    match = REFERENCE_PATTERN.search(reference)
    if not match:
        raise ConversionError(f"Unsupported NITE child reference {reference!r} in {source}")
    first_id, last_id = match.groups()
    if first_id not in positions or (last_id and last_id not in positions):
        missing = first_id if first_id not in positions else last_id
        raise ConversionError(f"Unresolved word ID {missing!r} in {source}")
    first_position = positions[first_id]
    last_position = positions[last_id] if last_id else first_position
    if last_position < first_position:
        raise ConversionError(f"Reverse word range {reference!r} in {source}")
    return [token.source_id for token in tokens[first_position : last_position + 1]]


def _join_tokens(tokens: list[Token]) -> str:
    text = ""
    for token in tokens:
        if not token.text:
            continue
        if token.punctuation:
            text += token.text
        elif not text:
            text = token.text
        else:
            text += f" {token.text}"
    return text


def _find_input_files(source_dir: Path, meeting_id: str) -> list[tuple[str, Path, Path]]:
    words_dir = source_dir / "words"
    segments_dir = source_dir / "segments"
    if not words_dir.is_dir() or not segments_dir.is_dir():
        raise ConversionError(f"Expected words/ and segments/ under {source_dir}")
    pairs: list[tuple[str, Path, Path]] = []
    for words_path in sorted(words_dir.glob(f"{meeting_id}.*.words.xml")):
        match = FILE_PATTERN.match(words_path.name)
        if not match or match.group("meeting") != meeting_id:
            continue
        speaker_id = match.group("speaker")
        segments_path = segments_dir / f"{meeting_id}.{speaker_id}.segments.xml"
        if not segments_path.is_file():
            raise ConversionError(f"Missing matching segments file for {words_path.name}")
        pairs.append((speaker_id, words_path, segments_path))
    if not pairs:
        raise ConversionError(f"No AMI word files found for {meeting_id} in {words_dir}")
    return pairs


def _parse_segments(
    path: Path,
    speaker_id: str,
    tokens: list[Token],
    positions: dict[str, int],
    warnings: list[str],
) -> list[dict[str, Any]]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ConversionError(f"Invalid XML in {path}") from exc
    turns: list[dict[str, Any]] = []
    for order, element in enumerate(root):
        if _local_name(element.tag) != "segment":
            continue
        segment_id = _nite_id(element)
        references = [child.get("href") for child in element if _local_name(child.tag) == "child"]
        if len(references) != 1 or not references[0]:
            raise ConversionError(f"Segment {segment_id} in {path} must contain one child reference")
        ids = _reference_ids(references[0], tokens, positions, path)
        segment_tokens = [tokens[positions[source_id]] for source_id in ids]
        start = _float_attribute(element, "transcriber_start")
        end = _float_attribute(element, "transcriber_end")
        if start is not None and end is not None and end < start:
            warnings.append(f"Segment {segment_id} has end before start time.")
        for token in segment_tokens:
            if token.start is not None and token.end is not None and token.end < token.start:
                warnings.append(f"Token {token.source_id} has end before start time.")
        turns.append(
            {
                "turn_id": segment_id,
                "speaker_id": speaker_id,
                "start": start,
                "end": end,
                "text": _join_tokens(segment_tokens),
                "tokens": [token.as_dict() for token in segment_tokens],
                "provenance": {
                    "segment_id": segment_id,
                    "page": None,
                    "paragraph": None,
                    "source_element_ids": ids,
                },
                "_order": order,
            }
        )
    return turns


def _source_file_metadata(source_dir: Path, files: list[Path]) -> list[dict[str, Any]]:
    metadata = []
    for file_path in files:
        metadata.append(
            {
                "path": str(file_path.relative_to(source_dir)),
                "sha256": sha256(file_path.read_bytes()).hexdigest(),
                "bytes": file_path.stat().st_size,
            }
        )
    return metadata


def validate_transcript(transcript: dict[str, Any]) -> list[str]:
    """Return schema-level validation errors for the canonical transcript contract."""
    errors: list[str] = []
    required = {"schema_version", "meeting_id", "source", "participants", "turns", "conversion"}
    missing = sorted(required.difference(transcript))
    if missing:
        errors.append(f"Missing top-level fields: {', '.join(missing)}")
        return errors
    if transcript["schema_version"] != SCHEMA_VERSION:
        errors.append(f"Unsupported schema version {transcript['schema_version']!r}")
    seen_turn_ids: set[str] = set()
    previous_start = float("-inf")
    for index, turn in enumerate(transcript["turns"]):
        for field in ("turn_id", "speaker_id", "start", "end", "text", "provenance"):
            if field not in turn:
                errors.append(f"Turn {index} is missing {field}")
        turn_id = turn.get("turn_id")
        if turn_id in seen_turn_ids:
            errors.append(f"Duplicate turn ID {turn_id}")
        seen_turn_ids.add(turn_id)
        start = turn.get("start")
        end = turn.get("end")
        if (start is None) != (end is None):
            errors.append(f"Turn {turn_id} must provide both timestamps or neither")
            continue
        if start is None:
            continue
        if not isinstance(start, (float, int)) or not isinstance(end, (float, int)):
            errors.append(f"Turn {turn_id} timestamps must be numeric or null")
            continue
        if start < previous_start:
            errors.append(f"Timed turns are not ordered at {turn_id}")
        previous_start = start
        if end < start:
            errors.append(f"Turn {turn_id} ends before it starts")
    return errors


def convert_ami_meeting(source_dir: Path, meeting_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return canonical transcript, raw parse data, and a conversion report."""
    source_dir = source_dir.expanduser().resolve()
    warnings: list[str] = []
    all_turns: list[dict[str, Any]] = []
    input_files: list[Path] = []
    speakers: list[str] = []
    token_counts: Counter[str] = Counter()

    for speaker_id, words_path, segments_path in _find_input_files(source_dir, meeting_id):
        speakers.append(speaker_id)
        input_files.extend((words_path, segments_path))
        tokens, positions = _parse_words(words_path)
        token_counts.update(token.kind for token in tokens)
        all_turns.extend(_parse_segments(segments_path, speaker_id, tokens, positions, warnings))

    all_turns.sort(key=lambda turn: (turn["start"], turn["speaker_id"], turn["_order"]))
    turn_ids = [turn["turn_id"] for turn in all_turns]
    if len(turn_ids) != len(set(turn_ids)):
        raise ConversionError("Segment IDs are not unique across speaker files")

    cleaned_turns = []
    for turn in all_turns:
        cleaned_turns.append({key: value for key, value in turn.items() if key not in {"tokens", "_order"}})

    file_metadata = _source_file_metadata(source_dir, input_files)
    transcript = {
        "schema_version": SCHEMA_VERSION,
        "meeting_id": meeting_id,
        "meeting_date": None,
        "timezone": None,
        "language": "en",
        "source": {
            "dataset": "AMI manual annotations 1.6.2",
            "input_format": "ami_xml",
            "files": [entry["path"] for entry in file_metadata],
        },
        "participants": [{"speaker_id": speaker_id, "display_name": None} for speaker_id in speakers],
        "turns": cleaned_turns,
        "conversion": {
            "method": "deterministic",
            "parser_version": PARSER_VERSION,
            "llm_normalization_used": False,
            "warnings": sorted(set(warnings)),
        },
    }
    errors = validate_transcript(transcript)
    if errors:
        raise ConversionError("Canonical transcript validation failed: " + "; ".join(errors))
    raw = {
        "schema_version": SCHEMA_VERSION,
        "meeting_id": meeting_id,
        "source": transcript["source"],
        "turns": [{key: value for key, value in turn.items() if key != "_order"} for turn in all_turns],
    }
    report = {
        "meeting_id": meeting_id,
        "parser_version": PARSER_VERSION,
        "speaker_count": len(speakers),
        "speakers": speakers,
        "turn_count": len(cleaned_turns),
        "token_counts": dict(sorted(token_counts.items())),
        "lexical_word_count": token_counts["word"],
        "unresolved_reference_count": 0,
        "warnings": sorted(set(warnings)),
    }
    manifest = {
        "meeting_id": meeting_id,
        "source_files": file_metadata,
        "artifacts": [],
        "parser_version": PARSER_VERSION,
    }
    return transcript, raw, {"report": report, "manifest": manifest}


def write_ami_artifacts(source_dir: Path, meeting_id: str, output_dir: Path) -> dict[str, Any]:
    """Convert one meeting and write all Phase 1 review artifacts."""
    transcript, raw, metadata = convert_ami_meeting(source_dir, meeting_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, bytes] = {
        "transcript.raw.json": json.dumps(raw, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n",
        "transcript.cleaned.json": json.dumps(transcript, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n",
        "transcript.txt": render_text(transcript).encode("utf-8"),
        "transcript.md": render_markdown(transcript).encode("utf-8"),
        "transcript.csv": render_csv(transcript).encode("utf-8"),
        "transcript.docx": render_docx(transcript),
        "transcript.pdf": render_pdf(transcript),
    }
    for filename, content in artifacts.items():
        (output_dir / filename).write_bytes(content)
    metadata["manifest"]["artifacts"] = [
        {"path": filename, "sha256": sha256(content).hexdigest(), "bytes": len(content)}
        for filename, content in sorted(artifacts.items())
    ]
    (output_dir / "conversion_report.json").write_text(
        json.dumps(metadata["report"], indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "source_manifest.json").write_text(
        json.dumps(metadata["manifest"], indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return transcript
