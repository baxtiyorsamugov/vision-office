"""Optional per-attempt recognition trace (replay bench only).

The event store keeps one unknown event per track per cooldown window, so it
cannot show how a person's score develops while they walk to the turnstile.
When VISION_OFFICE_RECOGNITION_TRACE is set, every recognition attempt is
appended as one JSON line: track, face crop size, best person, second-best
*different* person and the decision. Production never sets this variable.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import cv2
import numpy as np

CROP_FLOOR = 0.35


def trace_enabled() -> bool:
    return os.getenv("VISION_OFFICE_RECOGNITION_TRACE", "").strip().lower() in {"1", "true", "yes", "on"}


class RecognitionTrace:
    def __init__(self, camera_id: str, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in camera_id)[:100]
        self._file = (directory / f"{safe}.jsonl").open("a", encoding="utf-8")
        self._crops = directory / "crops" / safe
        self._crops.mkdir(parents=True, exist_ok=True)
        self._root = directory.parent.parent  # project root: data/recognition_trace -> project
        self.camera_id = camera_id
        self._seq = 0

    def write(self, *, track_id, face_shape, identities, similarities, threshold, decision, chosen=None,
              face_img=None) -> None:
        record = {"t": round(time.time(), 3), "camera": self.camera_id, "track": int(track_id) if track_id is not None else None,
                  "face_w": int(face_shape[1]) if face_shape is not None else None,
                  "face_h": int(face_shape[0]) if face_shape is not None else None,
                  "threshold": round(float(threshold), 3), "decision": decision}
        if similarities is not None and len(similarities):
            order = np.argsort(-similarities)[:50]
            best = None
            for index in order:
                identity = identities[int(index)]
                key = str(identity.get("person_id") or identity.get("name"))
                if best is None:
                    best = (key, identity, float(similarities[int(index)]))
                elif key != best[0]:
                    record["second_id"] = key
                    record["second_score"] = round(float(similarities[int(index)]), 4)
                    break
            if best is not None:
                record["best_id"], record["best_name"], record["best_score"] = best[0], best[1].get("name"), round(best[2], 4)
        if chosen is not None:
            record["chosen_id"] = chosen.get("person_id")
        # Keep the crop of plausible attempts so a voting decision can be checked by eye.
        if face_img is not None and record.get("best_score", 0) >= CROP_FLOOR:
            self._seq += 1
            target = self._crops / f"{int(record['t'] * 1000)}_{record['track']}_{self._seq}.jpg"
            if cv2.imwrite(str(target), face_img, [cv2.IMWRITE_JPEG_QUALITY, 90]):
                record["crop"] = target.relative_to(self._root).as_posix()
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()
