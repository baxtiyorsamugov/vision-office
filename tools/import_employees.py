import pathlib
import sys
import cv2
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.ai.recognizer import FaceRecognizer

TOKEN = (ROOT / "data" / "admin_access_token.txt").read_text(encoding="utf-8").strip()
FOLDER = ROOT / "data" / "imports" / "xodimlar" / "Xodimlar"
CENTER = "78e17fab-e021-5553-bb51-7840fe68355f"
BASE = "https://tatibaev.uz/api/v1"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

existing_response = requests.get(
    f"{BASE}/learning-centers/{CENTER}/persons",
    headers=HEADERS,
    params={"person_type": "employee", "limit": 200},
    timeout=20,
)
existing_response.raise_for_status()
existing = {item.get("fio") for item in existing_response.json()}
recognizer = FaceRecognizer()

for photo in sorted(FOLDER.iterdir()):
    name = photo.stem
    if name in existing:
        print(f"SKIP {name}", flush=True)
        continue
    image = cv2.imread(str(photo))
    embedding = recognizer.get_embedding(image)
    if embedding is None:
        raise RuntimeError(f"No usable face in {photo.name}")
    with photo.open("rb") as source:
        uploaded = requests.post(
            f"{BASE}/images/upload",
            headers=HEADERS,
            files={"file": (photo.name, source, "image/jpeg")},
            timeout=30,
        )
    uploaded.raise_for_status()
    upload_payload = uploaded.json()
    if isinstance(upload_payload, str):
        photo_url = upload_payload
    elif isinstance(upload_payload, dict):
        photo_url = (
            upload_payload.get("url")
            or upload_payload.get("photo_url")
            or upload_payload.get("path")
            or upload_payload.get("file_url")
        )
    else:
        photo_url = None
    if not isinstance(photo_url, str) or not photo_url:
        raise RuntimeError(f"Unexpected image-upload response: {upload_payload!r}")
    created = requests.post(
        f"{BASE}/learning-centers/{CENTER}/persons",
        headers=HEADERS,
        json={"person_type": "employee", "fio": name, "photo_url": photo_url},
        timeout=20,
    )
    created.raise_for_status()
    person_id = created.json()["id"]
    response = requests.put(
        f"{BASE}/learning-centers/{CENTER}/persons/{person_id}/embedding",
        headers=HEADERS,
        json={"embedding": embedding.astype(float).tolist()},
        timeout=30,
    )
    response.raise_for_status()
    print(f"OK {name}", flush=True)
