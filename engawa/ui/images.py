"""キャラクターの画像（アイコン・立ち絵）の読み込み。

コアの /characters が返す相対パスを、パッケージ内の assets から読む。
画像がないキャラクターでも動くよう、アイコンは頭文字の丸で代用する。
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPainterPath, QPixmap

from engawa.characters import ASSET_DIR, DEFAULT_EXPRESSION


def _device_ratio() -> float:
    screen = QGuiApplication.primaryScreen()
    return screen.devicePixelRatio() if screen is not None else 1.0


@lru_cache(maxsize=32)
def _load_image(relative: str) -> QImage | None:
    path = ASSET_DIR / relative
    image = QImage(str(path)) if path.is_file() else QImage()
    return None if image.isNull() else image


def _circle(size: int, paint) -> QPixmap:
    ratio = _device_ratio()
    pixmap = QPixmap(round(size * ratio), round(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addEllipse(QRectF(0, 0, size, size))
    painter.setClipPath(clip)
    paint(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


@lru_cache(maxsize=64)
def icon_pixmap(relative: str | None, size: int, fallback_text: str = "", color: str = "#5865f2") -> QPixmap:
    """丸く切り抜いたアイコン。画像がなければ、色付きの丸に頭文字を描く。"""
    image = _load_image(relative) if relative else None

    def paint(painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, QColor(color if image is None else "#2b2d31"))
        if image is not None:
            # 短い辺に合わせて中央を正方形に切り出す
            side = min(image.width(), image.height())
            source = QRectF((image.width() - side) / 2, 0, side, side)
            painter.drawImage(rect, image, source)
        elif fallback_text:
            font = QFont()
            font.setPixelSize(round(size * 0.45))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor("white"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, fallback_text[:1])

    return _circle(size, paint)


def standing_path(character: dict, expression: str = DEFAULT_EXPRESSION) -> str | None:
    """表情に対応する立ち絵。その表情がなければ既定の表情にする。"""
    standing = character.get("standing") or {}
    return standing.get(expression) or standing.get(DEFAULT_EXPRESSION)


@lru_cache(maxsize=16)
def standing_pixmap(relative: str | None, height: int) -> QPixmap | None:
    """高さを合わせて縮小した立ち絵。画像がなければ None。"""
    image = _load_image(relative) if relative else None
    if image is None:
        return None
    ratio = _device_ratio()
    scaled = image.scaledToHeight(round(height * ratio), Qt.TransformationMode.SmoothTransformation)
    pixmap = QPixmap.fromImage(scaled)
    pixmap.setDevicePixelRatio(ratio)
    return pixmap
