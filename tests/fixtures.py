from __future__ import annotations

from pathlib import Path


NITE = "http://nite.sourceforge.net/"


def write_ami_fixture(root: Path) -> None:
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
