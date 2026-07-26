"""Command-line interface for Phase 1 AMI preprocessing."""

from __future__ import annotations

import argparse
from pathlib import Path

from .ami import ConversionError, write_ami_artifacts


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert AMI annotations into Phase 1 transcript artifacts.")
    parser.add_argument("--source-dir", type=Path, required=True, help="AMI annotation root containing words/ and segments/")
    parser.add_argument("--meeting-id", required=True, help="AMI meeting ID, for example ES2002a")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory to receive generated artifacts")
    arguments = parser.parse_args()
    try:
        transcript = write_ami_artifacts(arguments.source_dir, arguments.meeting_id, arguments.output_dir)
    except ConversionError as exc:
        parser.error(str(exc))
    print(f"Wrote {len(transcript['turns'])} turns to {arguments.output_dir}")


if __name__ == "__main__":
    main()
