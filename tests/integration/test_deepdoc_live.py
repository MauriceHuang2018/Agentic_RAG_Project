"""Live integration tests against a real DeepDoc Server container.

These tests require a running DeepDoc Server reachable at
``$DEEPDOC_LIVE_URL``.  They are skipped unless that environment variable is
set, so the standard CI loop (``pytest``) stays decoupled from Docker state.

Run manually:

    PowerShell::

        $env:DEEPDOC_LIVE_URL = "http://localhost:9390"
        .venv/Scripts/python.exe -m pytest tests/integration/test_deepdoc_live.py -v

    bash::

        DEEPDOC_LIVE_URL=http://localhost:9390 \
            .venv/Scripts/python.exe -m pytest tests/integration/test_deepdoc_live.py -v

What these tests cover (compared with the existing
``tests/test_parser_router.py`` mock-transport suite):

    1. ``GET /health`` returns the canonical DeepDoc ``"ok"`` body.
    2. ``POST /predict/dla`` returns a non-empty box list for a real JPEG.
    3. The format dispatcher routes a raw JPEG to the DeepDoc visual router.
    4. The format dispatcher routes a scanned PDF to the DeepDoc visual router.
    5. The format dispatcher routes a text-layer PDF to PyMuPDF (no OCR flag).
    6. The format dispatcher routes a DOCX to python-docx (no OCR flag).
    7. The format dispatcher routes a PPTX to python-pptx.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_rag_project.doc_processor.parser import (
    DeepDocClient,
    parse_document_with_router,
)
from agentic_rag_project.doc_processor.visual_router import DeepDocVisualRouter

# Resolved at import time so pytest can short-circuit via ``pytestmark.skipif``.
LIVE_URL: str | None = os.environ.get("DEEPDOC_LIVE_URL")

# ``test_samples/`` sits at the project root, two parents above this file.
SAMPLES_DIR: Path = Path(__file__).resolve().parents[2] / "test_samples"


pytestmark = pytest.mark.skipif(
    LIVE_URL is None,
    reason="DEEPDOC_LIVE_URL not set; live DeepDoc integration test skipped",
)


# ---- helpers ----------------------------------------------------------------


def _first(glob_pattern: str) -> Path:
    """Resolve a single test sample by glob.

    Test fixtures in ``test_samples/`` carry CJK / space-bearing filenames
    (``康波周期表.jpg`` etc.).  Hardcoding those literals into a Python
    source file goes through a different encoding path on Windows than the
    ``read_bytes()`` call below, which surfaces as ``FileNotFoundError``.
    Globbing the filesystem instead bypasses the literal entirely — the
    resolved ``Path`` comes straight from the OS.
    """
    matches = sorted(SAMPLES_DIR.glob(glob_pattern))
    assert matches, f"no test samples matched {glob_pattern!r} in {SAMPLES_DIR}"
    return matches[0]


# ---- fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> DeepDocClient:
    """A module-scoped DeepDoc client so the underlying httpx pool is reused."""
    assert LIVE_URL is not None  # guarded by pytestmark above
    return DeepDocClient(base_url=LIVE_URL)


@pytest.fixture(scope="module")
def visual_router(client: DeepDocClient) -> DeepDocVisualRouter:
    """Visual router with ``fallback_ocr`` enabled.

    Real-world DeepDoc pages frequently contain layout regions the OCR-rec
    model cannot read (low-contrast charts, scanned figures, ...).  When
    that happens DLA returns boxes but every text block comes back empty,
    and ``parse_images`` raises ``RuntimeError``.  ``fallback_ocr=True``
    exercises the production fallback path (RapidOCR) so the end-to-end
    dispatcher still produces a usable ``ParsedDoc``.
    """
    return DeepDocVisualRouter(client=client, fallback_ocr=True)


# ---- tests ------------------------------------------------------------------


def test_live_health_ok(client: DeepDocClient) -> None:
    """The standalone DeepDoc Server returns ``200 ok`` on ``GET /health``."""
    response = client._client.get(f"{LIVE_URL}/health")
    assert response.status_code == 200
    assert response.text.strip().lower() == "ok"


def test_live_predict_dla_on_image(client: DeepDocClient) -> None:
    """``POST /predict/dla`` returns at least one layout box for a real JPEG.

    The real DeepDoc LitServe server returns the box list under the
    ``bboxes`` key (older / mock fixtures use ``boxes``).  Accept either.
    """
    image_path = _first("*.jpg")
    payload = client.predict_dla(image_path.read_bytes(), filename=image_path.name)
    assert isinstance(payload, dict)
    boxes = payload.get("boxes") or payload.get("bboxes") or []
    assert len(boxes) >= 1, "expected at least one layout box from /predict/dla"
    # Each DeepDoc DLA box is [x0, y0, x1, y1, score, class_id] (6 fields).
    for box in boxes:
        assert isinstance(box, (list, tuple))
        assert len(box) >= 4, f"box too short: {box!r}"


def test_live_dispatcher_image_routes_to_visual_router(
    visual_router: DeepDocVisualRouter,
) -> None:
    """A raw JPEG must end up on the DeepDoc visual path (``ocr_used=True``)."""
    image_path = _first("*.jpg")
    doc = parse_document_with_router(image_path, visual_router=visual_router)
    assert doc.sections, "visual router produced no sections for the JPEG"
    assert doc.ocr_used is True


def test_live_dispatcher_scanned_pdf_routes_to_visual_router(
    visual_router: DeepDocVisualRouter,
) -> None:
    """A scanned PDF must be detected and routed to the DeepDoc visual path."""
    pdf_path = SAMPLES_DIR / "scanned_pdf.pdf"  # ASCII filename — no encoding risk
    assert pdf_path.exists(), f"missing sample: {pdf_path}"
    doc = parse_document_with_router(pdf_path, visual_router=visual_router)
    assert doc.sections, "visual router produced no sections for the scanned PDF"
    assert doc.ocr_used is True


def test_live_dispatcher_text_pdf_uses_pymupdf() -> None:
    """A text-layer PDF must stay on the PyMuPDF fast path (no OCR flag)."""
    pdf_path = sorted(SAMPLES_DIR.glob("*任正非*.pdf"))[0]
    assert pdf_path.exists()
    doc = parse_document_with_router(pdf_path)
    assert doc.sections, "PyMuPDF extractor produced no sections for the text PDF"
    assert doc.ocr_used is False, "text-layer PDF should not have been OCRed"


def test_live_dispatcher_docx_uses_python_docx() -> None:
    """A DOCX must be handled in-process by python-docx."""
    docx_path = sorted(SAMPLES_DIR.glob("*.docx"))[0]
    assert docx_path.exists()
    doc = parse_document_with_router(docx_path)
    assert doc.sections, "python-docx extractor produced no sections"
    assert doc.ocr_used is False, "DOCX should not have triggered the OCR path"


def test_live_dispatcher_pptx_uses_python_pptx() -> None:
    """A PPTX must be handled in-process by python-pptx (one section per slide)."""
    pptx_path = sorted(SAMPLES_DIR.glob("*.pptx"))[0]
    assert pptx_path.exists()
    doc = parse_document_with_router(pptx_path)
    assert doc.sections, "python-pptx extractor produced no sections"
    assert doc.page_count >= 1, "PPTX should report at least one slide as a page"
    assert doc.ocr_used is False, "PPTX should not have triggered the OCR path"