from __future__ import annotations

import argparse
from pathlib import Path

from app.release_validation import write_backend_release_validation_summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate backend release validation summary markdown.")
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output path. Defaults to docs/release_reports/backend_release_validation_summary.md.",
    )
    args = parser.parse_args()

    output_path = write_backend_release_validation_summary(Path(args.output) if args.output else None)
    print(f"Wrote backend release validation summary to {output_path}")


if __name__ == "__main__":
    main()
