"""Shared helpers for the 4-stage PDF smoke scripts.

DESIGN  parser_router/DESIGN_parser_router.md (commit F6-L20)

Each stage script (`01_parse.py` ... `04_index.py`) is a stand-alone CLI
that can be invoked independently. Stages communicate via JSON files in
the same directory, so a user can stop after any stage and inspect the
intermediate `ParsedDoc` / `ParentChunk` / `EmbeddedChunk` payload before
proceeding.

Naming convention:
    parsed.json     <- written by 01_parse.py, read by 02_chunk.py
    chunks.json     <- written by 02_chunk.py, read by 03_embed.py
    embedded.json   <- written by 03_embed.py, read by 04_index.py

Path resolution uses ``glob`` instead of hard-coding CJK filenames so the
same scripts work whether the only sample on disk is the ASCII-named
``scanned_pdf.pdf`` or a Chinese-titled research report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Resolve the project root from this file so the scripts run from any cwd.
PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]
SAMPLES_DIR: Path = PROJECT_ROOT / "test_samples"

# Intermediate-state files written by each stage.
PARSED_FILE: Path = Path(__file__).resolve().parent / "parsed.json"
CHUNKS_FILE: Path = Path(__file__).resolve().parent / "chunks.json"
EMBEDDED_FILE: Path = Path(__file__).resolve().parent / "embedded.json"

# Defaults — read from environment so the user can override per-run.
DEFAULT_DEEPDOC_URL: str = "http://localhost:9390"
DEFAULT_QDRANT_HOST: str = "localhost"
DEFAULT_QDRANT_PORT: int = 6333


def resolve_sample(suffix: str | tuple[str, ...] = "*.pdf") -> Path:
    """Return the first sample file matching ``suffix`` under ``test_samples/``.

    Accepts a single glob pattern (``"*.docx"``) or a tuple of patterns
    (``("*.jpg", "*.jpeg", "*.png")``). Results are sorted so the pick is
    deterministic across runs — hard-coding a CJK filename (``任正非...pdf``)
    goes through a different Windows encoding path than ``Path.read_bytes()``
    and surfaces as a spurious ``FileNotFoundError``; globbing the OS
    bypasses that hazard.
    """
    if not SAMPLES_DIR.exists():
        sys.exit(f"test_samples directory not found: {SAMPLES_DIR}")
    patterns = (suffix,) if isinstance(suffix, str) else suffix
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(sorted(SAMPLES_DIR.glob(pattern)))
    if not matches:
        sys.exit(f"no samples matching {patterns!r} in {SAMPLES_DIR}")
    return matches[0]


def resolve_pdf_sample() -> Path:
    """Backwards-compatible shim — prefer ``resolve_sample("*.pdf")``."""
    return resolve_sample("*.pdf")


def require_file(path: Path, label: str) -> None:
    """Exit with a helpful message if ``path`` is missing."""
    if not path.exists():
        sys.exit(
            f"{label} not found: {path}\n"
            f"Run the preceding stage first to produce this file."
        )


def load_json(path: Path) -> dict[str, Any]:
    """Read a UTF-8 JSON file and return the decoded object."""
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as UTF-8 JSON with stable, readable formatting."""
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)