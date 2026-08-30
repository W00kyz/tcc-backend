import re
import uuid
import zlib

from app.domain.qr.crypto import sign_qr_payload
from app.domain.qr.models import QrCode, QrCodeStatus
from app.domain.qr.pdf import _PUBLIC_CODE_FONT_SIZE, generate_qr_sheet_pdf
from fpdf import FPDF

# Matches a PDF literal string operand immediately followed by the Tj text-show operator,
# e.g. "(v1.abc...==) Tj" — this is what fpdf2 actually draws on the page, as opposed to the
# string that was passed to cell()/multi_cell() (which may get wrapped into several such
# operators, or may pass through as a single one that overflows the page uncut).
_TJ_LITERAL = re.compile(r"\(((?:[^()\\]|\\.)*)\)\s*Tj")


def test_generate_qr_sheet_pdf_returns_a_valid_pdf() -> None:
    floor_id = uuid.uuid4()
    payload = sign_qr_payload(floor_id=floor_id, version=1, private_key_hex="11" * 32)
    qr_code = QrCode(
        floor_id=floor_id,
        public_code=payload,
        secret=b"11" * 32,
        version=1,
        status=QrCodeStatus.ACTIVE,
    )

    pdf_bytes = generate_qr_sheet_pdf(
        building_name="Bloco CI", floor_label="Térreo", qr_code=qr_code
    )

    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 500


def _drawn_text_runs(pdf_bytes: bytes) -> list[str]:
    """Decompresses every FlateDecode content stream in a rendered PDF and returns each
    literal string fpdf2 drew via a Tj operator, in emission order. Reading back what was
    actually drawn (rather than re-deriving it from the same wrapping call the code under
    test uses) is what makes this catch a regression in generate_qr_sheet_pdf itself, not
    just in fpdf2's wrapping primitive."""
    runs: list[str] = []
    for stream_match in re.finditer(rb"stream\r?\n(.*?)endstream", pdf_bytes, re.DOTALL):
        try:
            content = zlib.decompress(stream_match.group(1)).decode("latin-1")
        except zlib.error:
            continue
        runs.extend(_TJ_LITERAL.findall(content))
    return runs


def test_public_code_is_drawn_across_lines_that_fit_the_printable_page_width() -> None:
    """Regression test for a real bug: public_code is "v1.<uuid>.<version>.<ed25519-sig-
    b64url>", always 130 characters with no spaces to break on. Drawn with fpdf2's `cell`
    (one line, no wrap), that line measures ~228-235mm (varies with the specific glyphs in a
    given payload — Helvetica is proportional) against an A4 page only 190mm wide printable —
    confirmed, by rendering the actual generated PDF to a 300dpi PNG and running
    `pdftotext -layout` on it, to truncate both the leading "v1." and the trailing "==" when
    actually printed. This calls the real generate_qr_sheet_pdf() and inspects the actual PDF
    bytes it returns (see _drawn_text_runs), so reverting `multi_cell` back to `cell` in
    pdf.py makes it fail: only one (too-wide) run would be drawn instead of several."""
    floor_id = uuid.uuid4()
    payload = sign_qr_payload(floor_id=floor_id, version=1, private_key_hex="11" * 32)
    qr_code = QrCode(
        floor_id=floor_id,
        public_code=payload,
        secret=b"11" * 32,
        version=1,
        status=QrCodeStatus.ACTIVE,
    )

    pdf_bytes = generate_qr_sheet_pdf(
        building_name="Bloco CI", floor_label="Térreo", qr_code=qr_code
    )

    # building_name/floor_label/the version-date line share no characters with public_code's
    # base64url+hex+dot alphabet, so filtering drawn runs down to substrings of payload
    # isolates exactly the runs that came from rendering it.
    public_code_runs = [run for run in _drawn_text_runs(pdf_bytes) if run and run in payload]

    assert len(public_code_runs) > 1, (
        "expected public_code to be drawn as multiple wrapped lines, found "
        f"{len(public_code_runs)} run(s) — did generate_qr_sheet_pdf revert to a "
        "non-wrapping `cell` call for public_code?"
    )
    assert "".join(public_code_runs) == payload, "drawing must not drop or alter any character"

    measuring_pdf = FPDF(orientation="P", unit="mm", format="A4")
    measuring_pdf.add_page()
    measuring_pdf.set_font("Helvetica", size=_PUBLIC_CODE_FONT_SIZE)
    printable_width = measuring_pdf.w - measuring_pdf.l_margin - measuring_pdf.r_margin
    for run in public_code_runs:
        assert measuring_pdf.get_string_width(run) <= printable_width
