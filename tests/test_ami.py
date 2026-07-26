from __future__ import annotations

import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meeting_preprocessor.ami import convert_ami_meeting, write_ami_artifacts


NITE = "http://nite.sourceforge.net/"


class AmiConversionTests(unittest.TestCase):
    def _write_fixture(self, root: Path) -> None:
        (root / "words").mkdir(parents=True)
        (root / "segments").mkdir()
        (root / "words" / "TEST.A.words.xml").write_text(
            f'''<?xml version="1.0" encoding="ISO-8859-1"?>
<nite:root xmlns:nite="{NITE}" nite:id="TEST.A.words">
  <w nite:id="TEST.A.words0" starttime="1.0" endtime="1.1">Hello</w>
  <w nite:id="TEST.A.words1" starttime="1.1" endtime="1.1" punc="true">,</w>
  <w nite:id="TEST.A.words2" starttime="1.1" endtime="1.3">world</w>
  <w nite:id="TEST.A.words3" starttime="1.3" endtime="1.3" punc="true">!</w>
  <vocalsound nite:id="TEST.A.words4" starttime="1.4" endtime="1.5" type="laugh"/>
</nite:root>''',
            encoding="iso-8859-1",
        )
        (root / "segments" / "TEST.A.segments.xml").write_text(
            f'''<?xml version="1.0" encoding="ISO-8859-1"?>
<nite:root xmlns:nite="{NITE}" nite:id="TEST.A.segs">
  <segment nite:id="TEST.sync.1" transcriber_start="1.0" transcriber_end="1.3">
    <nite:child href="TEST.A.words.xml#id(TEST.A.words0)..id(TEST.A.words3)"/>
  </segment>
  <segment nite:id="TEST.sync.2" transcriber_start="1.4" transcriber_end="1.5">
    <nite:child href="TEST.A.words.xml#id(TEST.A.words4)"/>
  </segment>
</nite:root>''',
            encoding="iso-8859-1",
        )

    def test_convert_preserves_turns_punctuation_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_fixture(root)
            transcript, raw, metadata = convert_ami_meeting(root, "TEST")
        self.assertEqual(transcript["meeting_id"], "TEST")
        self.assertEqual(transcript["turns"][0]["text"], "Hello, world!")
        self.assertEqual(transcript["turns"][1]["text"], "[laugh]")
        self.assertEqual(transcript["turns"][0]["provenance"]["source_element_ids"], [
            "TEST.A.words0", "TEST.A.words1", "TEST.A.words2", "TEST.A.words3"
        ])
        self.assertEqual(len(raw["turns"][0]["tokens"]), 4)
        self.assertEqual(metadata["report"]["unresolved_reference_count"], 0)

    def test_all_artifacts_are_created_and_agree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            output = Path(temporary) / "output"
            self._write_fixture(root)
            write_ami_artifacts(root, "TEST", output)
            expected = {
                "source_manifest.json", "transcript.raw.json", "transcript.cleaned.json", "transcript.txt",
                "transcript.md", "transcript.csv", "transcript.docx", "transcript.pdf", "conversion_report.json",
            }
            self.assertEqual({path.name for path in output.iterdir()}, expected)
            transcript = json.loads((output / "transcript.cleaned.json").read_text())
            csv_rows = list(csv.DictReader((output / "transcript.csv").read_text().splitlines()))
            self.assertEqual([row["text"] for row in csv_rows], [turn["text"] for turn in transcript["turns"]])
            docx = (output / "transcript.docx").read_bytes()
            pdf = (output / "transcript.pdf").read_bytes()
            self.assertTrue(docx.startswith(b"PK"))
            self.assertTrue(pdf.startswith(b"%PDF-"))
            with zipfile.ZipFile(BytesIO(docx)) as archive:
                self.assertIn(b"Hello, world!", archive.read("word/document.xml"))
            self.assertIn(b"Hello, world!", pdf)

    def test_repeated_conversion_is_byte_for_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            self._write_fixture(root)
            write_ami_artifacts(root, "TEST", first)
            write_ami_artifacts(root, "TEST", second)
            self.assertEqual(
                {path.name: path.read_bytes() for path in first.iterdir()},
                {path.name: path.read_bytes() for path in second.iterdir()},
            )

    def test_es2002a_golden_turn_when_source_is_available(self) -> None:
        source = Path("/Users/audrey/Desktop/Agentic project/ami_public_manual_1.6.2")
        if not source.is_dir():
            self.skipTest("AMI source dataset is not available in this environment")
        transcript, _, _ = convert_ami_meeting(source, "ES2002a")
        golden = json.loads((Path(__file__).parent / "golden" / "es2002a_first_turn.json").read_text())
        self.assertEqual(transcript["turns"][0], golden)
