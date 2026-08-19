from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPen


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "assets" / "app_icon.ico"


def main():
    app = QGuiApplication([])
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(256, 256, QImage.Format_ARGB32)
    image.fill(Qt.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#3E328A"))
    painter.drawRoundedRect(QRectF(8, 8, 240, 240), 48, 48)

    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(QColor("#FFFFFF"), 16, Qt.SolidLine, Qt.RoundCap))
    painter.drawEllipse(QRectF(62, 52, 132, 132))

    painter.setPen(QPen(QColor("#FFFFFF"), 12, Qt.SolidLine, Qt.RoundCap))
    for start, end in (
        ((128, 28), (128, 44)),
        ((54, 74), (42, 62)),
        ((202, 74), (214, 62)),
        ((42, 130), (26, 130)),
        ((214, 130), (230, 130)),
    ):
        painter.drawLine(*start, *end)

    painter.setPen(QColor("#FFFFFF"))
    font = QFont("Segoe UI", 36, QFont.Bold)
    painter.setFont(font)
    painter.drawText(QRectF(28, 175, 200, 56), Qt.AlignCenter, "ONE4ALL")
    painter.end()

    if not image.save(str(OUTPUT), "ICO"):
        raise RuntimeError("Qt could not create the Windows ICO file")
    print(f"Created {OUTPUT}")
    app.quit()


if __name__ == "__main__":
    main()
