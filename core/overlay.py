"""Unicode-safe OpenCV overlays for employee names in Cyrillic and Latin.

Only the small label area is rendered with Pillow; the full frame is never
converted, so the cost per face stays tiny even on 2560x1440 cameras.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


FONT_SIZE = 22
_FONT = None
_LABEL_CACHE: "OrderedDict[tuple, np.ndarray]" = OrderedDict()
_LABEL_CACHE_SIZE = 256

# BGR colors by recognition state.
KNOWN_COLOR = (0, 180, 90)
UNKNOWN_COLOR = (40, 40, 220)
PENDING_COLOR = (0, 200, 230)
PENDING_NAMES = {"Анализ...", "Поиск лица...", "Стабилизация..."}
UNKNOWN_NAMES = {"Неизвестный"}

FONT_CANDIDATES = (
    # Bundled with the project, so Docker (Linux) images render Cyrillic too.
    Path(__file__).resolve().parent / "assets" / "fonts" / "DejaVuSans-Bold.ttf",
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("C:/Windows/Fonts/segoeui.ttf"),
)


def _font():
    global _FONT
    if _FONT is not None:
        return _FONT
    for candidate in FONT_CANDIDATES:
        if candidate.is_file():
            _FONT = ImageFont.truetype(str(candidate), FONT_SIZE)
            return _FONT
    _FONT = ImageFont.load_default()
    return _FONT


def label_color(text: str) -> tuple[int, int, int]:
    if text in UNKNOWN_NAMES:
        return UNKNOWN_COLOR
    if text in PENDING_NAMES:
        return PENDING_COLOR
    return KNOWN_COLOR


def _render_label(text: str, color: tuple[int, int, int]) -> np.ndarray:
    key = (text, color)
    cached = _LABEL_CACHE.get(key)
    if cached is not None:
        _LABEL_CACHE.move_to_end(key)
        return cached
    font = _font()
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    left, top, right, bottom = probe.textbbox((0, 0), text, font=font)
    width, height = right - left + 14, bottom - top + 10
    rgb = (color[2], color[1], color[0])
    image = Image.new("RGB", (max(1, width), max(1, height)), rgb)
    text_rgb = (255, 255, 255) if color != KNOWN_COLOR and color != PENDING_COLOR else (0, 0, 0)
    ImageDraw.Draw(image).text((7 - left, 5 - top), text, font=font, fill=text_rgb)
    label = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
    _LABEL_CACHE[key] = label
    if len(_LABEL_CACHE) > _LABEL_CACHE_SIZE:
        _LABEL_CACHE.popitem(last=False)
    return label


def draw_detection_label(frame: np.ndarray, box, text: str, confidence: float | None = None) -> np.ndarray:
    """Draw a box and a compact label in place; returns the same frame."""
    x1, y1, x2, y2 = (int(value) for value in box)
    text = str(text)
    color = label_color(text)
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    shown = f"{text} {confidence:.2f}" if confidence is not None else text
    label = _render_label(shown, color)
    frame_h, frame_w = frame.shape[:2]
    lh, lw = label.shape[:2]
    top = y1 - lh if y1 - lh >= 0 else min(frame_h - lh, y2)
    left = min(max(0, x1), max(0, frame_w - lw))
    top = max(0, top)
    h = min(lh, frame_h - top)
    w = min(lw, frame_w - left)
    if h > 0 and w > 0:
        frame[top:top + h, left:left + w] = label[:h, :w]
    return frame
