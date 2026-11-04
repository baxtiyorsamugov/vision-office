"""Simulate track-level voting on the replay recognition trace (no worker changes).

Today a track is recognised only if ONE attempt scores above the threshold. The
voting rule under test: a not-yet-recognised track is assigned to person P when
at least K attempts have P as the best person with score >= FLOOR and a gap of
at least MARGIN to the best *other* person, and no other person collects K such
votes. Conflicts (votes for someone other than the recognised person, or two
people with K votes) measure the risk of false acceptances.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

RULES = [(0.45, 2, 0.05), (0.45, 3, 0.05), (0.42, 3, 0.08), (0.40, 3, 0.10), (0.45, 2, 0.08)]
DEFAULT_RULE = (0.45, 2, 0.05)
TRACK_GAP_SECONDS = 20.0


def load_trace(directory: Path, since_ts: float) -> list[dict]:
    attempts = []
    for path in sorted(directory.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as source:
            for line in source:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("t", 0) >= since_ts:
                    attempts.append(record)
    return sorted(attempts, key=lambda r: r["t"])


def split_tracks(attempts: list[dict]) -> list[list[dict]]:
    """Group by camera+track id; a long pause starts a new track (ids restart with the worker)."""
    tracks: dict[tuple, list[list[dict]]] = defaultdict(list)
    for record in attempts:
        key = (record.get("camera"), record.get("track"))
        groups = tracks[key]
        if not groups or record["t"] - groups[-1][-1]["t"] > TRACK_GAP_SECONDS:
            groups.append([])
        groups[-1].append(record)
    return [group for groups in tracks.values() for group in groups]


def margin(record: dict) -> float:
    best = record.get("best_score")
    if best is None:
        return 0.0
    return best - record.get("second_score", 0.0)


def votes(track: list[dict], floor: float, min_margin: float) -> Counter:
    return Counter(r["best_id"] for r in track
                   if r.get("best_id") and r.get("best_score", 0) >= floor and margin(r) >= min_margin)


def evaluate(track: list[dict], rule) -> dict:
    floor, k, min_margin = rule
    recognised = next((r.get("chosen_id") for r in track if r.get("decision") == "known"), None)
    ballot = votes(track, floor, min_margin)
    strong = [person for person, count in ballot.items() if count >= k]
    outcome = {"recognised": recognised, "voted": None, "conflict": False}
    if recognised:
        outcome["conflict"] = any(person != recognised for person in strong)
    elif len(strong) == 1:
        outcome["voted"] = strong[0]
    elif len(strong) > 1:
        outcome["conflict"] = True
    return outcome


def analyse(directory: Path, since_ts: float) -> dict:
    attempts = load_trace(directory, since_ts)
    tracks = [t for t in split_tracks(attempts) if any(r.get("best_score", 0) >= 0.30 for r in t)]
    results = []
    for rule in RULES:
        outcomes = [evaluate(track, rule) for track in tracks]
        results.append({
            "rule": rule,
            "tracks": len(tracks),
            "recognised": sum(1 for o in outcomes if o["recognised"]),
            "gained": sum(1 for o in outcomes if o["voted"]),
            "conflicts": sum(1 for o in outcomes if o["conflict"]),
        })
    gained = []
    for track in tracks:
        outcome = evaluate(track, DEFAULT_RULE)
        if outcome["voted"]:
            mine = sorted((r for r in track if r.get("best_id") == outcome["voted"] and r.get("crop")),
                          key=lambda r: -r.get("best_score", 0))
            gained.append({
                "camera": track[0].get("camera"), "track": track[0].get("track"), "person_id": outcome["voted"],
                "name": mine[0].get("best_name") if mine else outcome["voted"],
                "attempts": len(track), "best_score": max(r.get("best_score", 0) for r in track),
                "crops": [r["crop"] for r in mine[:3]],
                "second": sorted({(r.get("second_id"), round(r.get("second_score", 0), 2)) for r in mine[:3]}),
            })
    sizes = [min(r.get("face_w") or 0, r.get("face_h") or 0) for r in attempts]
    per_track_attempts = [len(t) for t in tracks]
    return {
        "attempts": len(attempts),
        "tracks": len(tracks),
        "median_attempts_per_track": sorted(per_track_attempts)[len(per_track_attempts) // 2] if per_track_attempts else 0,
        "no_embedding_share": round(sum(1 for r in attempts if r.get("decision") == "no-embedding") / len(attempts), 3) if attempts else 0,
        "small_faces_share": round(sum(1 for s in sizes if s < 80) / len(sizes), 3) if sizes else 0,
        "rules": results,
        "gained": gained,
    }
