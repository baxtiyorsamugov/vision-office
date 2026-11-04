"""Summarise what the replay bench recognised and explain the misses.

Reads ONLY the replay database (never production). For every unknown face it
compares the stored observation embedding with the same gallery the live
worker uses and shows the three closest people, so a miss can be classified:

* near-miss: best score within 0.10 below the threshold -> usually angle/quality,
  or a missing reference from this camera; a reviewed reference often fixes it;
* weak: a plausible person but far below the threshold -> camera angle/size;
* far: nobody in the gallery is close -> outsider or unusable face.

Output: data/exports/replay-report-<stamp>/index.html (inside the replay data dir).
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NEAR_MISS_WINDOW = 0.10
WEAK_FLOOR = 0.30


def classify(best: float | None, threshold: float) -> str:
    if best is None:
        return "no-embedding"
    if best > threshold:
        return "above-threshold"
    if best >= threshold - NEAR_MISS_WINDOW:
        return "near-miss"
    if best >= WEAK_FLOOR:
        return "weak"
    return "far"


def top_candidates(embedding, names, vectors, limit=3):
    if embedding is None or vectors is None or not len(names):
        return []
    vector = np.asarray(embedding, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    if vector.shape != (vectors.shape[1],) or not np.isfinite(norm) or norm == 0:
        return []
    sims = vectors @ (vector / norm)
    best_by_person: dict[str, tuple[float, dict]] = {}
    for index in np.argsort(-sims):
        identity = names[int(index)]
        key = str(identity.get("person_id") or identity.get("name"))
        if key not in best_by_person:
            best_by_person[key] = (float(sims[int(index)]), identity)
        if len(best_by_person) >= limit:
            break
    return [{"name": item["name"], "person_id": item.get("person_id"), "score": round(score, 3)}
            for score, item in best_by_person.values()]


def threshold_for(identity_id: str | None, edge_threshold: float, edu_threshold: float) -> float:
    if str(identity_id or "").startswith("edu:"):
        return max(edge_threshold, edu_threshold)
    return edge_threshold


def load_gallery():
    from sqlalchemy.orm import sessionmaker

    from core.ai.engine import _load_known_faces
    from core.edge.config import load_edge_settings
    from core.edge.service import EdgeService
    from database.manager import get_engine

    edge_settings = load_edge_settings()
    engine = get_engine()
    edge_service = EdgeService(edge_settings, engine=engine) if edge_settings.configured else None
    names, vectors = _load_known_faces(sessionmaker(bind=engine), edge_service, edge_settings.enabled)
    return engine, names, vectors


def photo_size(path: Path) -> tuple[int, int] | None:
    try:
        import cv2

        image = cv2.imread(str(path))
        if image is None:
            return None
        return int(image.shape[1]), int(image.shape[0])
    except Exception:
        return None


def collect(engine, names, vectors, since, edge_threshold, edu_threshold):
    from sqlalchemy.orm import sessionmaker

    from database.models import RecognitionEvent, UnknownFaceObservation

    session = sessionmaker(bind=engine)()
    try:
        rows = session.query(RecognitionEvent, UnknownFaceObservation.embedding).outerjoin(
            UnknownFaceObservation, UnknownFaceObservation.event_id == RecognitionEvent.id
        ).filter(
            RecognitionEvent.camera_id.like("replay_%"),
            RecognitionEvent.created_at >= since,
        ).order_by(RecognitionEvent.created_at).all()
        events = []
        for event, embedding in rows:
            candidates = top_candidates(embedding, names, vectors) if event.person_id is None else []
            best = candidates[0] if candidates else None
            threshold = threshold_for(best["person_id"] if best else event.person_id, edge_threshold, edu_threshold)
            size = photo_size(ROOT / event.photo_path) if event.photo_path else None
            events.append({
                "id": event.id,
                "camera_id": event.camera_id,
                "created_at": event.created_at.isoformat(),
                "known": event.person_id is not None,
                "person_name": event.person_name,
                "confidence": event.confidence,
                "photo_path": event.photo_path,
                "photo_size": size,
                "candidates": candidates,
                # Gap between the best and the second-best person: a large gap means no confusion.
                "margin": round(candidates[0]["score"] - candidates[1]["score"], 3) if len(candidates) > 1 else None,
                "best_person_id": event.person_id or (best["person_id"] if best else None),
                "best_name": event.person_name if event.person_id else (best["name"] if best else None),
                "threshold": threshold,
                "category": "recognised" if event.person_id else classify(best["score"] if best else None, threshold),
            })
        return events
    finally:
        session.close()


PERSON_ORDER = ["recognised", "near-miss", "weak", "far", "no-embedding", "above-threshold"]


def people_summary(events):
    """Group observations by person: the recognised identity, or the best candidate of a miss.

    'far' faces are not attributed to anybody (their best candidate is noise), so
    they are counted as faces only. A person is 'recognised' if any observation
    was recognised; otherwise the best category reached across observations.
    """
    people: dict[tuple, dict] = {}
    for item in events:
        if item["category"] in ("far", "no-embedding") or not item.get("best_person_id"):
            continue
        key = (item["camera_id"], item["best_person_id"])
        person = people.setdefault(key, {
            "camera_id": item["camera_id"], "person_id": item["best_person_id"], "name": item.get("best_name"),
            "observations": 0, "recognised": 0, "near_miss": 0, "best_score": 0.0, "min_margin": None,
            "threshold": item["threshold"], "photo_path": item["photo_path"], "best_category": item["category"],
        })
        person["observations"] += 1
        score = item["confidence"] if item["known"] else (item["candidates"][0]["score"] if item["candidates"] else 0.0)
        if item["known"]:
            person["recognised"] += 1
        if item["category"] == "near-miss":
            person["near_miss"] += 1
            if item.get("margin") is not None:
                person["min_margin"] = item["margin"] if person["min_margin"] is None else min(person["min_margin"], item["margin"])
        rank = (bool(item["known"]), float(score or 0))
        if item["photo_path"] and rank > person.get("_photo_rank", (False, -1.0)):
            person["photo_path"], person["_photo_rank"] = item["photo_path"], rank
        person["best_score"] = max(person["best_score"], float(score or 0))
        if PERSON_ORDER.index(item["category"]) < PERSON_ORDER.index(person["best_category"]):
            person["best_category"] = item["category"]
    return sorted(people.values(), key=lambda p: (PERSON_ORDER.index(p["best_category"]), -p["best_score"]))


def voting_section(since: datetime) -> str:
    try:
        from tools.replay.voting import DEFAULT_RULE, analyse
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location("replay_voting", Path(__file__).with_name("voting.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        DEFAULT_RULE, analyse = module.DEFAULT_RULE, module.analyse
    trace_dir = ROOT / "data" / "recognition_trace"
    if not trace_dir.is_dir():
        return "<h2>Голосование по треку</h2><p>Нет журнала попыток (data/recognition_trace).</p>"
    data = analyse(trace_dir, since.timestamp())
    rows = "".join(
        f"<tr><td>score ≥ {r['rule'][0]:.2f}, голосов ≥ {r['rule'][1]}, отрыв ≥ {r['rule'][2]:.2f}</td>"
        f"<td>{r['tracks']}</td><td>{r['recognised']}</td><td><b>+{r['gained']}</b></td><td>{r['conflicts']}</td></tr>"
        for r in data["rules"])
    cards = "".join(
        "<figure>" + "".join(f'<img loading="lazy" src="../../{html.escape(c.removeprefix("data/"))}">' for c in g["crops"][:1])
        + f'<figcaption><b>{html.escape(str(g["name"]))}</b> (лучший {g["best_score"]:.2f})<br>'
        + "".join(f'<img class="thumb" src="../../{html.escape(c.removeprefix("data/"))}">' for c in g["crops"][1:3])
        + f'<br><small>{html.escape(str(g["camera"]))} · трек {g["track"]} · попыток {g["attempts"]}<br>второй: '
        + html.escape(", ".join(f"{n} {sc:.2f}" for n, sc in g["second"])) + "</small></figcaption></figure>"
        for g in data["gained"])
    return f"""<h2>Голосование по треку (симуляция, прод не меняется)</h2>
<p>Все попытки распознавания: {data['attempts']}, треков с лицом: {data['tracks']}, медиана попыток на трек: {data['median_attempts_per_track']},
попыток без эмбеддинга (InsightFace не нашёл лицо в кропе): <b>{100 * data['no_embedding_share']:.0f}%</b>,
доля попыток с кропом мельче 80 px: {100 * data['small_faces_share']:.0f}%.<br>
Сейчас трек узнан, если хоть одна попытка выше порога. Правило: иначе засчитать человека, если у него ≥ K попыток со score ≥ FLOOR и отрывом от второго человека ≥ MARGIN.
«Конфликт» = голоса за другого человека, чем узнанный, или за двух людей сразу (риск ложного).</p>
<table><tr><th>Правило</th><th>Треков</th><th>Узнано сейчас</th><th>Добавит голосование</th><th>Конфликтов</th></tr>{rows}</table>
<p>Кого добавит правило по умолчанию (score ≥ {DEFAULT_RULE[0]:.2f}, ≥ {DEFAULT_RULE[1]} голоса, отрыв ≥ {DEFAULT_RULE[2]:.2f}). <b>Проверь глазами: это действительно этот человек?</b></p>
<div class="grid">{cards or "<p>Никого.</p>"}</div>"""


def run_params() -> str:
    path = ROOT / "data" / "run_params.txt"
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return "неизвестно"


def render(events, destination: Path, since: datetime) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    per_camera: dict[str, Counter] = defaultdict(Counter)
    people: dict[str, set] = defaultdict(set)
    for item in events:
        per_camera[item["camera_id"]][item["category"]] += 1
        if item["known"]:
            people[item["camera_id"]].add(item["person_name"])
    categories = ["recognised", "near-miss", "weak", "far", "no-embedding", "above-threshold"]
    summary_rows = []
    for camera, counts in sorted(per_camera.items()):
        total = sum(counts.values())
        cells = "".join(f"<td>{counts.get(c, 0)}</td>" for c in categories)
        summary_rows.append(f"<tr><td>{html.escape(camera)}</td><td>{total}</td>{cells}<td>{len(people[camera])}</td></tr>")

    persons = people_summary(events)
    person_counts: dict[str, Counter] = defaultdict(Counter)
    for person in persons:
        person_counts[person["camera_id"]][person["best_category"]] += 1
    far_faces: Counter = Counter(e["camera_id"] for e in events if e["category"] in ("far", "no-embedding"))
    people_rows = []
    for camera in sorted(per_camera):
        counts = person_counts[camera]
        attributed = sum(counts.values())
        rate = f'{100 * counts.get("recognised", 0) / attributed:.0f}%' if attributed else "-"
        people_rows.append(
            f"<tr><td>{html.escape(camera)}</td><td>{attributed}</td><td><b>{counts.get('recognised', 0)}</b></td>"
            f"<td>{rate}</td><td>{counts.get('near-miss', 0)}</td><td>{counts.get('weak', 0)}</td><td>{far_faces[camera]}</td></tr>")

    def person_row(person):
        image = ""
        if person["photo_path"]:
            image = f'<img class="thumb" loading="lazy" src="../../{html.escape(person["photo_path"].removeprefix("data/"))}">'
        margin = f'{person["min_margin"]:.2f}' if person["min_margin"] is not None else ""
        return (f'<tr class="{person["best_category"]}"><td>{image}</td><td>{html.escape(person["name"] or "")}</td>'
                f'<td>{html.escape(person["camera_id"])}</td><td class="cat {person["best_category"]}">{person["best_category"]}</td>'
                f'<td>{person["best_score"]:.2f}</td><td>{person["threshold"]:.2f}</td><td>{person["observations"]}</td>'
                f'<td>{person["recognised"]}</td><td>{person["near_miss"]}</td><td>{margin}</td></tr>')

    def card(item):
        image = ""
        if item["photo_path"]:
            image = f'<img loading="lazy" src="../../{html.escape(item["photo_path"].removeprefix("data/"))}">'
        size = f'{item["photo_size"][0]}x{item["photo_size"][1]}px' if item["photo_size"] else "?"
        if item["known"]:
            title = f'{html.escape(item["person_name"] or "")} <b>{(item["confidence"] or 0):.2f}</b>'
        else:
            guesses = "<br>".join(f'{html.escape(c["name"])} {c["score"]:.2f}' for c in item["candidates"]) or "нет эмбеддинга"
            title = f'<span class="cat {item["category"]}">{item["category"]}</span><br>{guesses}'
        return (f'<figure data-cat="{item["category"]}" data-cam="{html.escape(item["camera_id"])}">{image}'
                f'<figcaption>{title}<br><small>{html.escape(item["camera_id"])} · {item["created_at"][11:19]} · {size} · порог {item["threshold"]:.2f}</small></figcaption></figure>')

    ordered = sorted(events, key=lambda e: (e["known"], -(e["candidates"][0]["score"] if e["candidates"] else -1)))
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Replay report</title><style>
body{{font:14px system-ui,sans-serif;margin:16px;background:#fafafa;color:#222}}
table{{border-collapse:collapse;margin-bottom:16px}}td,th{{border:1px solid #ccc;padding:4px 8px;text-align:right}}td:first-child{{text-align:left}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}}
figure{{margin:0;background:#fff;border:1px solid #ddd;border-radius:6px;padding:6px}}img{{width:100%;height:160px;object-fit:contain;background:#eee}}
.cat{{font-weight:600}}.near-miss{{color:#b45309}}.weak{{color:#a16207}}.far{{color:#b91c1c}}
button{{margin:0 6px 10px 0}}.thumb{{width:48px;height:60px;object-fit:cover}}table.people td{{vertical-align:middle}}
tr.recognised td:nth-child(4){{color:#15803d;font-weight:600}}
</style></head><body>
<h1>Replay: результаты распознавания</h1>
<p><b>Параметры запуска:</b> {html.escape(run_params())}</p>
<h2>По людям</h2>
<p>Человек = узнанная личность или лучший кандидат промаха (near-miss/weak). «far» лица не приписываются никому и считаются отдельно как лица.
«Узнан» = хотя бы одно наблюдение выше порога. Запуск с {since.isoformat(timespec="seconds")}.</p>
<table><tr><th>Камера</th><th>Людей</th><th>Узнано</th><th>Доля</th><th>Только near-miss</th><th>Только weak</th><th>far-лиц (наблюдений)</th></tr>{"".join(people_rows)}</table>
<details open><summary>Список людей ({len(persons)})</summary>
<table class="people"><tr><th></th><th>Кандидат</th><th>Камера</th><th>Итог</th><th>Лучший score</th><th>Порог</th><th>Наблюдений</th><th>Узнан раз</th><th>near-miss раз</th><th>Мин. отрыв от 2-го</th></tr>
{"".join(person_row(p) for p in persons)}</table></details>
{voting_section(since)}
<h2>Все наблюдения</h2>
<p>Всего: {len(events)}. Это наблюдения (одно лицо может дать несколько событий), а не люди.
near-miss = лучший кандидат в пределах {NEAR_MISS_WINDOW:.2f} ниже порога; weak = {WEAK_FLOOR:.2f}…near-miss; far = ниже {WEAK_FLOOR:.2f}.</p>
<table><tr><th>Камера</th><th>Всего</th>{"".join(f"<th>{c}</th>" for c in categories)}<th>Разных узнанных</th></tr>{"".join(summary_rows)}</table>
<div>{"".join(f'<button onclick="f(this.textContent)">{c}</button>' for c in ["all"] + categories)}</div>
<div class="grid">{"".join(card(e) for e in ordered)}</div>
<script>function f(c){{document.querySelectorAll('figure').forEach(x=>x.style.display=(c==='all'||x.dataset.cat===c)?'':'none')}}</script>
</body></html>"""
    (destination / "index.html").write_text(page, encoding="utf-8")
    (destination / "events.json").write_text(json.dumps(events, ensure_ascii=False, indent=1), encoding="utf-8")
    return destination / "index.html"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=0, help="Only the last N minutes (0 = everything)")
    parser.add_argument("--since", default="", help="Only events after this UTC time (ISO), e.g. the bench start")
    args = parser.parse_args(argv)

    from core.edge.config import load_edge_settings
    from core.eduschool.catalog import load_settings as load_eduschool_settings

    if args.minutes > 0:
        since = datetime.now(timezone.utc) - timedelta(minutes=args.minutes)
    elif args.since:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    else:
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    engine, names, vectors = load_gallery()
    events = collect(engine, names, vectors, since,
                     load_edge_settings().recognition_threshold, load_eduschool_settings().recognition_threshold)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = render(events, ROOT / "data" / "exports" / f"replay-report-{stamp}", since)
    counts = Counter(e["category"] for e in events)
    print(f"Gallery vectors: {len(names)}; replay events: {len(events)}; " + ", ".join(f"{k}={v}" for k, v in counts.most_common()))
    persons = people_summary(events)
    by_cat = Counter(p["best_category"] for p in persons)
    print(f"People: {len(persons)}; " + ", ".join(f"{k}={v}" for k, v in by_cat.most_common()))
    print(f"Report: replay/runtime/data/exports/{path.parent.name}/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
