from datetime import UTC, datetime
from io import BytesIO

import qrcode
from fpdf import FPDF
from qrcode.image.pil import PilImage

from app.domain.qr.models import QrCode

# public_code is "v1.<floor_id-uuid>.<version>.<ed25519-signature-base64url>" — always 130
# characters (fixed-length uuid + fixed-length base64url signature) with no spaces to break
# on, so at any readable font size it does not fit A4's ~190mm printable width on one line
# (measured single-line width at this size: ~228-235mm depending on the specific glyphs in a
# given payload — Helvetica is proportional, so the exact figure varies run to run). Rendered
# with multi_cell instead of cell, so fpdf2 hard-wraps the run across lines that do fit.
_PUBLIC_CODE_FONT_SIZE = 9


def generate_qr_sheet_pdf(*, building_name: str, floor_label: str, qr_code: QrCode) -> bytes:
    """RF09 — one printable A4 sheet per floor: building/floor label, the QR image, the public
    code in small text for manual double-checking, version and generation date."""
    # image_factory is pinned to PilImage: qrcode.make()'s default return type is the stub's
    # generic base image, whose save() signature (matching qrcode.image.pure.PyPNGImage) has
    # no `format` argument — mypy rejects `format="PNG"` against that inferred type even though
    # PilImage (what Pillow being installed actually selects at runtime) accepts it.
    qr_image = qrcode.make(qr_code.public_code, image_factory=PilImage)
    image_buffer = BytesIO()
    qr_image.save(image_buffer, format="PNG")
    image_buffer.seek(0)

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.add_page()
    pdf.set_font("Helvetica", style="B", size=18)
    # multi_cell (not cell) guards building_name/floor_label the same way as public_code below:
    # an unusually long name now wraps instead of silently running past the page edge. new_x
    # must be pinned back to LMARGIN — multi_cell's own default (XPos.RIGHT) would otherwise
    # leave x at the previous box's right edge, shrinking every w=0 box rendered after it.
    pdf.multi_cell(0, 12, text=building_name, new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", size=14)
    pdf.multi_cell(0, 10, text=floor_label, new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(10)
    pdf.image(image_buffer, x=65, y=pdf.get_y(), w=80)
    pdf.ln(90)
    pdf.set_font("Helvetica", size=_PUBLIC_CODE_FONT_SIZE)
    pdf.multi_cell(0, 6, text=qr_code.public_code, new_x="LMARGIN", new_y="NEXT", align="C")
    # The built-in "Helvetica" core font only supports Latin-1 (fpdf2's core_fonts_encoding),
    # which excludes the em-dash "—" (U+2014) the brief's original text used — an ASCII hyphen
    # fits Latin-1 and reads the same on a printed sheet.
    pdf.cell(
        0,
        6,
        text=f"Versão {qr_code.version} - gerado em {datetime.now(UTC):%d/%m/%Y %H:%M}",
        new_x="LMARGIN",
        new_y="NEXT",
        align="C",
    )

    return bytes(pdf.output())
