import cv2
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
import yaml
from PIL import Image, ImageDraw, ImageFont

from core.video.streamer import VideoStream
from core.ai.engine import AI_Engine
from core.hr import HRManager


@lru_cache(maxsize=1)
def get_label_font():
    font_paths = (
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/tahoma.ttf"),
    )
    for font_path in font_paths:
        if font_path.is_file():
            return ImageFont.truetype(str(font_path), 22)
    return ImageFont.load_default()


def draw_label(frame, text, x, y):
    """Draw a UTF-8 label on a small frame region to keep rendering light."""
    font = get_label_font()
    left = max(0, int(x))
    label_bottom = max(0, int(y))
    text_box = font.getbbox(text)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    padding = 6
    label_top = max(0, label_bottom - text_height - padding * 2)
    label_right = min(frame.shape[1], left + text_width + padding * 2)

    if label_right <= left or label_bottom <= label_top:
        return

    cv2.rectangle(frame, (left, label_top), (label_right, label_bottom), (0, 255, 0), -1)
    label_region = frame[label_top:label_bottom, left:label_right]
    label_image = Image.fromarray(cv2.cvtColor(label_region, cv2.COLOR_BGR2RGB))
    ImageDraw.Draw(label_image).text(
        (padding, padding - text_box[1]),
        text,
        font=font,
        fill=(0, 0, 0),
    )
    label_region[:] = cv2.cvtColor(np.asarray(label_image), cv2.COLOR_RGB2BGR)


def main():
    with open("config/settings.yaml", "r") as f:
        cfg = yaml.safe_load(f)
    
    cam_url = cfg['cameras'][0]['rtsp_url']
    
    print("🚀 Запуск боевого AI Engine...")
    ai = AI_Engine()
    hr = HRManager(cooldown_minutes=1)
    
    print(f"📡 Подключение к: {cam_url}")
    stream = VideoStream(cam_url).start()

    try:
        while True:
            frame = stream.read()
            if frame is None:
                time.sleep(0.005)
                continue

            frame = cv2.resize(frame, (1280, 720))
            results = ai.process_frame(frame)

            for item in results:
                x1, y1, x2, y2 = item['box']
                name = item['name']
                hr.register_presence(name)

                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                draw_label(frame, name, x1, y1)

            cv2.imshow("Smart Vision AI - RTSP LIVE", frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
    finally:
        stream.stop()
        ai.stop()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
