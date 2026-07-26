from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meeting_preprocessor.ami import write_ami_artifacts
from meeting_preprocessor.converter import InputValidationError, TranscriptConverter
from tests.fixtures import write_ami_fixture


class ConverterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source = root / "source"
        self.generated = root / "generated"
        self.uploads = root / "uploads"
        self.artifacts = root / "artifacts"
        write_ami_fixture(self.source)
        write_ami_artifacts(self.source, "TEST", self.generated)
        self.uploads.mkdir()
        self.converter = TranscriptConverter(self.uploads, self.artifacts)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _upload(self, filename: str) -> None:
        shutil.copy2(self.generated / filename, self.uploads / filename)

    def test_detect_rejects_paths_outside_upload_root(self) -> None:
        with self.assertRaises(InputValidationError):
            self.converter.detect_document_type("../source/words/TEST.A.words.xml")

    def test_canonical_json_bypasses_conversion(self) -> None:
        self._upload("transcript.cleaned.json")
        validation = self.converter.validate_json_artifact("transcript.cleaned.json")
        result = self.converter.convert_document("transcript.cleaned.json")
        self.assertTrue(validation["valid"])
        self.assertEqual(result.transcript["conversion"]["method"], "deterministic")
        self.assertEqual(result.transcript["turns"][0]["text"], "Hello, world!")

    def test_ami_directory_converts_with_meeting_id(self) -> None:
        converter = TranscriptConverter(self.source, self.artifacts)
        result = converter.convert_document(".", meeting_id="TEST")
        self.assertEqual([turn["text"] for turn in result.transcript["turns"]], ["Hello, world!", "[laugh]"])

    def test_generated_representations_convert_to_equivalent_turn_text(self) -> None:
        expected = ["Hello, world!", "[laugh]"]
        for filename in ("transcript.txt", "transcript.md", "transcript.csv", "transcript.docx", "transcript.pdf"):
            with self.subTest(filename=filename):
                self._upload(filename)
                result = self.converter.convert_document(filename, meeting_id="TEST")
                self.assertEqual([turn["text"] for turn in result.transcript["turns"]], expected)
                self.assertEqual([turn["speaker_id"] for turn in result.transcript["turns"]], ["A", "A"])

    def test_render_and_report_read_stored_conversion(self) -> None:
        self._upload("transcript.txt")
        result = self.converter.convert_document("transcript.txt", meeting_id="TEST")
        render = self.converter.render_converted_transcript(result.conversion_id, "csv")
        report = self.converter.conversion_report(result.conversion_id)
        self.assertTrue((self.artifacts / render["artifact_path"]).is_file())
        self.assertEqual(report["turn_count"], 2)

    def test_mcp_stdio_server_exposes_and_calls_converter_tools(self) -> None:
        self._upload("transcript.txt")

        async def call_server() -> tuple[set[str], dict[str, object]]:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            parameters = StdioServerParameters(
                command="python3",
                args=["-m", "meeting_preprocessor.server"],
                cwd=Path(__file__).resolve().parents[1],
                env={
                    **os.environ,
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                    "CONVERTER_UPLOAD_ROOT": str(self.uploads),
                    "CONVERTER_ARTIFACT_ROOT": str(self.artifacts),
                },
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    result = await session.call_tool("detect_document_type", {"artifact_path": "transcript.txt"})
                    return {tool.name for tool in tools.tools}, json.loads(result.content[0].text)

        tools, result = asyncio.run(call_server())
        self.assertTrue({"detect_document_type", "convert_document", "validate_transcript", "render_transcript", "get_conversion_report"}.issubset(tools))
        self.assertEqual(result["document_type"], "text")
