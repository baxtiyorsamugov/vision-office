"""Build a local human-review bundle from an isolated PostgreSQL dump, never the app DB."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "tools" / "templates"


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def photo_path(root, stored):
    value = str(stored or "").replace("\\", "/")
    prefix = next((p for p in ("/app/data/", "data/") if value.startswith(p)), None)
    if prefix is None:
        raise ValueError("Unsupported photo path")
    relative = value[len(prefix):]
    if not relative.startswith(("events/", "persons/")) or ":" in relative or ".." in relative.split("/"):
        raise ValueError("Photo path outside imported events/persons")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Photo path escapes input root")
    return path


def event_person_id(row):
    kind, prefix = {"eduschool_employee": ("employee", "edu:e:"),
                    "eduschool_student": ("student", "edu:s:")}.get(row["person_type"], ("", ""))
    value = row.get("person_id") or ""
    return f"{kind}:{value[len(prefix):]}" if kind and value.startswith(prefix) else None


def command(*args):
    result = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"Command failed ({args[0]}): {result.stderr[-2500:]}")
    return result.stdout.strip()


@contextmanager
def isolated_dump(root, dump):
    image = command("docker", "image", "inspect", "postgres:16-alpine", "--format", "{{.Id}}")
    name = "acs-photo-review-" + uuid.uuid4().hex[:12]
    container = command("docker", "run", "-d", "--name", name,
        "--label", "acs.photo-review=" + name, "--network", "none", "--memory", "1536m", "--cpus", "2",
        "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=photo_review",
        "--mount", f"type=bind,source={root},target=/input,readonly", image)
    if not re.fullmatch(r"[0-9a-f]{64}", container):
        raise RuntimeError("Unexpected temporary container ID")
    try:
        info = json.loads(command("docker", "inspect", container))[0]
        mounts = info["Mounts"]
        if (info["HostConfig"]["NetworkMode"] != "none" or info["HostConfig"]["PortBindings"]
                or len(mounts) != 2 or not any(m["Destination"] == "/input" and not m["RW"] for m in mounts)):
            raise RuntimeError("Isolation checks failed")
        ready = False
        for _ in range(90):
            # The entrypoint's temporary initialization server accepts sockets only.
            probe = subprocess.run(["docker", "exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"], capture_output=True)
            if probe.returncode == 0:
                ready = True
                break
            time.sleep(1)
        if not ready:
            raise RuntimeError("Temporary PostgreSQL did not start")
        print("Restoring dump in network-isolated PostgreSQL...", flush=True)
        command("docker", "exec", container, "pg_restore", "--exit-on-error", "--no-owner", "--no-privileges",
                "-U", "postgres", "-d", "photo_review", "/input/" + dump.name)

        def query(sql):
            wrapped = "SELECT COALESCE(json_agg(t), '[]'::json) FROM (" + sql + ") t"
            return json.loads(command("docker", "exec", container, "psql", "-X", "-A", "-t", "-v",
                "ON_ERROR_STOP=1", "-U", "postgres", "-d", "photo_review", "-c", wrapped))

        yield query
    finally:
        # Only the container created above and its anonymous volume are removed.
        command("docker", "rm", "-f", "-v", container)
        print("Temporary database removed.", flush=True)


def snapshot_id(report):
    payload = {k: report[k] for k in ("version", "dump_sha256", "people", "events")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def render(output):
    report = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if report["snapshot_id"] != snapshot_id(report):
        raise ValueError("Snapshot integrity mismatch")
    template = (TEMPLATES / "photo_review.html").read_text(encoding="utf-8")
    script = (TEMPLATES / "photo_review.js").read_text(encoding="utf-8")
    style = (TEMPLATES / "photo_review.css").read_text(encoding="utf-8")
    html = template.replace("__STYLE__", style).replace("__SCRIPT__", script).replace(
        "__REPORT_JSON__", json.dumps(report, ensure_ascii=False).replace("<", "\\u003c"))
    (output / "index.html").write_text(html, encoding="utf-8")
    shutil.copytree(ROOT / "tools/assets/photo_review_icons", output / "icons", dirs_exist_ok=True)


def build(input_root, audit_dir, output):
    input_root = input_root.resolve()
    output = output.resolve()
    if output.exists() or output.is_relative_to(input_root):
        raise ValueError("Use a new output directory outside the imported data")
    summary = json.loads((audit_dir / "summary.json").read_text(encoding="utf-8"))
    details = json.loads((audit_dir / "details.json").read_text(encoding="utf-8"))
    dumps = list(input_root.glob("*.dump"))
    if len(dumps) != 1 or digest(dumps[0]) != summary["dump"]["sha256"]:
        raise ValueError("A single dump matching the audited snapshot is required")
    images = {r["path"]: r for r in details["images"] if r["status"] == "ok"}
    with isolated_dump(input_root, dumps[0]) as query:
        people = query("""SELECT id,full_name,person_type,active FROM eduschool_catalog_people
                          ORDER BY full_name,id""")
        references = query("SELECT id,person_id,photo_path,active FROM eduschool_reference_photos ORDER BY id")
        events = query("""SELECT id,camera_id,event_type,person_id,person_type,person_name,photo_path,created_at
            FROM recognition_events ORDER BY created_at DESC,id""")
    output.mkdir(parents=True)
    media = output / "media"
    media.mkdir()
    copied, skipped = {}, Counter()

    def materialize(stored):
        path = photo_path(input_root, stored)
        relative = path.relative_to(input_root).as_posix()
        audited = images.get(relative)
        if not audited or not path.is_file():
            return None
        if relative not in copied:
            if digest(path) != audited["sha256"]:
                raise ValueError("Image changed since audit: " + relative)
            target = media / (audited["sha256"] + ".jpg")
            if not target.exists():
                shutil.copy2(path, target)
            copied[relative] = {"image": "media/" + target.name, "source_path": "data/" + relative,
                "sha256": audited["sha256"], "width": audited["width"], "height": audited["height"]}
        return dict(copied[relative])

    lookup = {p["id"]: p for p in people}
    for person in people:
        person["references"] = []
    for reference in references:
        if reference["active"] and reference["person_id"] in lookup:
            photo = materialize(reference["photo_path"])
            if photo:
                lookup[reference["person_id"]]["references"].append({"id": reference["id"], **photo})
            else:
                skipped["references"] += 1
    observations = []
    for i, event in enumerate(events):
        if event["photo_path"]:
            photo = materialize(event["photo_path"])
        else:
            photo = None
        if photo is None:
            skipped["events"] += 1
            continue
        observations.append({"id": event["id"], "camera": event["camera_id"], "kind": event["event_type"],
            "at": event["created_at"], "prediction_name": event["person_name"],
            "prediction_type": event["person_type"], "prediction_person_id": event_person_id(event), **photo})
        if (i + 1) % 3000 == 0:
            print(f"Events processed: {i+1}/{len(events)}", flush=True)
    report = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "dump_sha256": summary["dump"]["sha256"], "people": people, "events": observations,
              "skipped": dict(skipped), "missing_by_date": summary.get("events_photo_coverage_by_folder_date", {})}
    report["snapshot_id"] = snapshot_id(report)
    (output / "manifest.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"events": len(observations), "people": len(people), "skipped": dict(skipped)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path)
    parser.add_argument("--audit-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-only", action="store_true")
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args()
    if args.data_only and args.render_only:
        parser.error("Choose one mode")
    if not args.render_only:
        if not args.input_root or not args.audit_dir:
            parser.error("--input-root and --audit-dir are required")
        build(args.input_root, args.audit_dir, args.output)
    if not args.data_only:
        render(args.output)


if __name__ == "__main__":
    main()
