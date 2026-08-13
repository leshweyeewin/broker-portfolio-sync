"""Rasterize a card SVG to PNG for direct upload (step 10 — §11b).

Lemon8/TikTok want an image, not vector markup. ``lemon8.card.render_card_svg``
already produces the card as SVG; this turns that SVG into PNG bytes.

Backend: ``svglib`` parses the SVG into a reportlab drawing, and reportlab's
``renderPM`` rasterizes it (via ``rlPyCairo`` + ``pycairo`` — the only combo that
installs cleanly on Windows without native Cairo DLLs). All heavy imports are
deferred into the function so the rest of the lemon8 package still imports when
these optional deps aren't installed.
"""

from __future__ import annotations

import io


class RenderError(RuntimeError):
    """Raised when an SVG can't be rasterized (bad SVG, or missing render deps)."""


def svg_to_png(svg: str) -> bytes:
    """Rasterize ``svg`` markup to PNG bytes. Fail loud on any error."""
    try:
        from svglib.svglib import svg2rlg
        from reportlab.graphics import renderPM
    except ImportError as exc:  # optional deps not installed
        raise RenderError(
            "PNG rendering needs svglib + reportlab + rlPyCairo + pycairo "
            "(pip install -r requirements.txt)."
        ) from exc

    drawing = svg2rlg(io.StringIO(svg))
    if drawing is None:
        raise RenderError("svglib could not parse the card SVG.")
    buf = io.BytesIO()
    try:
        renderPM.drawToFile(drawing, buf, fmt="PNG")
    except Exception as exc:  # reportlab raises assorted backend errors
        raise RenderError(f"PNG rasterization failed: {exc}") from exc
    return buf.getvalue()
