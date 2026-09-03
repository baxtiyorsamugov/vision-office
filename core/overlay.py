"""Unicode-safe OpenCV overlays for employee names in Cyrillic and Latin."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


_FONT = None


def _font(size: int):
    global _FONT
    if _FONT is not None:
        return _FONT
    candidates = (
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeui.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            _FONT = ImageFont.truetype(str(candidate), size)
            return _FONT
    _FONT = ImageFont.load_default()
    return _FONT


def draw_detection_label(frame: np.ndarray, box, text: str) -> np.ndarray:
    """Draw a compact label without replacing non-ASCII letters with question marks."""
    x1, y1, x2, y2 = (int(value) for value in box)
    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 180, 90), 2)
    font = _font(22)
    canvas = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(canvas)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    label_height = bottom - top + 10
    label_width = right - left + 14
    label_top = max(0, y1 - label_height)
    draw.rectangle((x1, label_top, x1 + label_width, y1), fill=(0, 180, 90))
    draw.text((x1 + 7, label_top + 4), text, font=font, fill=(0, 0, 0))
    return cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2BGR)
