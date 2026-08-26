"""Generate the GlobalNewsHub application icon (user request #2).

Paints a globe-on-gradient mark with QPainter and packs PNG-compressed
multiple sizes into ``resources/icons/globalnewshub.ico`` (PNG-in-ICO,
Vista+). Also writes a 512px PNG for docs. Idempotent; no assets needed.

Usage: .venv\\Scripts\\python scripts\\make_icon.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QGuiApplication,
    QIcon,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
)

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "resources" / "icons"
ICO_PATH = OUT_DIR / "globalnewshub.ico"
PNG_PATH = OUT_DIR / "globalnewshub_512.png"

_SIZES = [256, 128, 64, 48, 32, 16]


def _paint(size: int) -> QPixmap:
    """Render the mark at ``size`` square pixels."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size / 256.0  # design-space scale

    # Rounded gradient tile.
    grad = QLinearGradient(QPointF(0, 0), QPointF(size, size))
    grad.setColorAt(0.0, "#3b8bff")
    grad.setColorAt(1.0, "#0d3f85")
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(grad)
    painter.drawRoundedRect(QRectF(0, 0, size, size), 44 * s, 44 * s)

    cx, cy, r = 128 * s, 128 * s, 84 * s
    pen = QPen(Qt.GlobalColor.white, 11 * s)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    # Outer sphere.
    painter.drawEllipse(QPointF(cx, cy), r, r)
    # Meridians (two inner ellipses).
    painter.drawEllipse(QPointF(cx, cy), r * 0.62, r)
    painter.drawEllipse(QPointF(cx, cy), r * 0.24, r)
    # Parallels (three chords).
    for dy_ratio in (-0.55, 0.0, 0.55):
        dy = dy_ratio * r
        half = (r * r - dy * dy) ** 0.5
        painter.drawLine(QPointF(cx - half, cy + dy), QPointF(cx + half, cy + dy))

    # Newspaper corner accent: three white bars bottom-right.
    bars = [(196, 176, 34, 9), (196, 192, 26, 9), (196, 208, 18, 9)]
    for x, y, w, h in bars:
        painter.fillRect(QRectF(x * s, y * s, w * s, h * s),
                         Qt.GlobalColor.white)

    painter.end()
    return pix


def _png_bytes(size: int) -> bytes:
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    _paint(size).save(buffer, "PNG")
    return bytes(buffer.data())


def build_ico() -> Path:
    """Pack all sizes into a single PNG-compressed .ico container."""
    master = _paint(512)
    pngs = [
        _png_bytes(s) if s >= 256 else _scaled_png_bytes(master, s)
        for s in _SIZES
    ]

    out = bytearray()
    out += struct.pack("<HHH", 0, 1, len(_SIZES))  # reserved, type=icon, count
    offset = 6 + 16 * len(_SIZES)
    for size, blob in zip(_SIZES, pngs, strict=True):
        byte_size = size if size < 256 else 0
        out += struct.pack(
            "<BBBBHHII", byte_size, byte_size, 0, 0, 1, 32,
            len(blob), offset,
        )
        offset += len(blob)
    for blob in pngs:
        out += blob
    ICO_PATH.parent.mkdir(parents=True, exist_ok=True)
    ICO_PATH.write_bytes(bytes(out))
    return ICO_PATH


def _scaled_png_bytes(pix: QPixmap, size: int) -> bytes:
    """Smooth-downscale ``pix`` to ``size`` and return PNG bytes."""
    scaled = pix.scaled(
        size, size,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    scaled.save(buffer, "PNG")
    return bytes(buffer.data())


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    del app
    ico = build_ico()
    PNG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _paint(512).save(str(PNG_PATH), "PNG")

    # Sanity: Qt can read the container back with every size.
    icon = QIcon(str(ico))
    available = sorted({icon.availableSizes()[i].width()
                        for i in range(icon.availableSizes().__len__())})
    print(f"ico written: {ico} ({ico.stat().st_size:,} B), sizes={available}")
    print(f"png written: {PNG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
