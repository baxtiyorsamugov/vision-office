"""Import explicitly human-approved photos by exact ERP ID; dry-run by default."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)


def validate_bundle(folder, expected_count=None):
    from PIL import Image
    folder = Path(folder).resolve()
    manifest = read_json(folder / 'manifest.json')
    raw_review = (folder / 'original-human-review.json').read_bytes()
    review = read_json(folder / 'original-human-review.json')
    if (manifest.get('version') != 1 or manifest.get('purpose') != 'human_selected_reference_candidates'
            or manifest.get('applied') is not False or manifest.get('embeddings_computed') is not False
            or sha(raw_review) != manifest.get('source_annotation_sha256')
            or review.get('version') != 1 or review.get('purpose') != 'manual_photo_review'
            or review.get('applied') is not False
            or review.get('snapshot_id') != manifest.get('source_snapshot_id')
            or review.get('dump_sha256') != manifest.get('source_dump_sha256')):
        raise ValueError('Bundle does not match the original human review')
    approved = {k: v for k, v in review['decisions'].items()
                if v.get('status') == 'confirmed' and v.get('reference_approved') is True}
    items = manifest['items']
    if (not items or len(items) > 10000 or len(items) != len(approved)
            or (expected_count is not None and len(items) != expected_count)):
        raise ValueError('Unexpected approved photo count')
    seen, hashes = set(), set()
    for item in items:
        event_id = item['event_id']
        decision = approved.get(event_id)
        person_id = item['person_id']
        checksum = item['sha256']
        if (event_id in seen or checksum in hashes or not decision
                or item['human_decision'] != decision or decision.get('human_confirmed') is not True
                or decision.get('reason') is not None or decision.get('person_id') != person_id
                or decision.get('image_sha256') != checksum
                or not re.fullmatch(r'(employee|student):[0-9a-f]{24}', person_id)
                or item['person_type'] != person_id.split(':')[0]
                or not re.fullmatch(r'[0-9a-f]{64}', checksum)
                or item['image'] != f'photos/{checksum}.jpg'):
            raise ValueError('Invalid or repeated human-approved item')
        path = (folder / item['image']).resolve()
        if not path.is_relative_to(folder / 'photos') or path.is_symlink():
            raise ValueError('Photo path outside bundle')
        raw = path.read_bytes()
        if not 0 < len(raw) <= 10 * 1024 * 1024 or sha(raw) != checksum:
            raise ValueError('Photo checksum or size mismatch')
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != 'JPEG' or image.width * image.height > 16000000:
                raise ValueError('Unsupported image')
            if (image.width, image.height) != (item['width'], item['height']):
                raise ValueError('Photo dimensions changed')
            image.verify()
        seen.add(event_id)
        hashes.add(checksum)
    if seen != set(approved):
        raise ValueError('Bundle omits approved decisions')
    return manifest


def open_engine(url, *, readonly):
    from sqlalchemy import create_engine, event
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    if parsed.get_backend_name() == 'sqlite':
        path = Path(parsed.database).resolve()
        if not path.is_file():
            raise ValueError('Database must already exist')
        def connect():
            connection = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True)
            connection.execute('PRAGMA foreign_keys=ON')
            if readonly:
                connection.execute('PRAGMA query_only=ON')
            return connection
        return create_engine('sqlite://', creator=connect)
    if parsed.get_backend_name() != 'postgresql':
        raise ValueError('Only SQLite and PostgreSQL are supported')
    engine = create_engine(parsed)
    if readonly:
        @event.listens_for(engine, 'connect')
        def read_only(connection, _):
            if hasattr(connection, 'read_only'):
                connection.read_only = True
            else:
                connection.set_session(readonly=True)
    return engine


def preflight(engine, manifest, reference_limit, skip_inactive=False, skip_over_limit=False):
    from sqlalchemy import inspect, select
    from sqlalchemy.orm import Session
    from database.models import EduSchoolCatalogPerson as Person, EduSchoolReferencePhoto as Photo
    if not 1 <= reference_limit <= 30:
        raise ValueError('Invalid reference limit')
    required = {'eduschool_catalog_people', 'eduschool_reference_photos', 'employees',
                'remote_persons', 'remote_person_reference_photos'}
    if not required.issubset(inspect(engine).get_table_names()):
        raise ValueError('Required application tables are missing; no migrations will be run')
    with Session(engine) as session:
        people = {p.id: p for p in session.scalars(select(Person))}
        photos = list(session.scalars(select(Photo)))
        local_counts = Counter(p.person_id for p in photos if p.active and p.source == 'local')
        existing = {(p.person_id, p.image_checksum): p for p in photos}
        checksum_owners = {}
        for photo in photos:
            checksum_owners.setdefault(photo.image_checksum, set()).add(photo.person_id)
        results = []
        for item in manifest['items']:
            person = people.get(item['person_id'])
            duplicate = existing.get((item['person_id'], item['sha256']))
            status, reason = 'ready', None
            if person is None or not person.active:
                # An archived profile is never matched, so an operator may knowingly leave it out.
                status = 'skipped_inactive' if skip_inactive else 'blocked'
                reason = 'profile_missing_or_inactive'
            elif (person.person_type != item['person_type']
                  or person.id != person.person_type + ':' + person.external_id
                  or person.full_name != item['full_name']):
                status, reason = 'blocked', 'profile_metadata_changed'
            elif duplicate:
                status = 'already_present' if duplicate.active else 'already_present_inactive'
            elif checksum_owners.get(item['sha256'], set()) - {item['person_id']}:
                status, reason = 'blocked', 'same_file_assigned_to_other_profile'
            elif local_counts[item['person_id']] >= reference_limit:
                # Manifest order decides which photos fit; the rest are left out only on request.
                status = 'skipped_limit' if skip_over_limit else 'blocked'
                reason = 'local_reference_limit'
            else:
                local_counts[item['person_id']] += 1
            results.append({'event_id': item['event_id'], 'person_id': item['person_id'],
                            'sha256': item['sha256'], 'status': status, 'reason': reason,
                            'existing_reference_id': duplicate.id if duplicate else None})
    return results


def backup_references(engine, root, manifest, destination):
    """Scoped recovery snapshot; application tables and old files are never changed."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from database.models import EduSchoolCatalogPerson as Person, EduSchoolReferencePhoto as Photo
    ids = {r['person_id'] for r in manifest['items']}
    def record(row):
        return {c.name: getattr(row, c.name) for c in row.__table__.columns}
    with Session(engine) as session:
        people = [record(p) for p in session.scalars(select(Person).where(Person.id.in_(ids)))]
        photos = [record(p) for p in session.scalars(select(Photo).where(Photo.person_id.in_(ids)))]
    files = {}
    for photo in photos:
        path = (root / photo['photo_path']).resolve()
        if not path.is_relative_to((root / 'data/persons').resolve()):
            raise ValueError('Existing reference path is outside data/persons')
        raw = path.read_bytes()
        files[path.relative_to(root).as_posix()] = raw
    with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('import-manifest.json', json.dumps(manifest, ensure_ascii=False))
        archive.writestr('records.json', json.dumps({'scope': 'affected_catalog_cards_and_references_only',
            'people': people, 'references': photos, 'file_sha256': {k: sha(v) for k, v in files.items()},
            'review_sha256': manifest['source_annotation_sha256']}, default=str))
        for name, raw in files.items():
            archive.writestr(name, raw)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError('Backup verification failed')
        for name, raw in files.items():
            if sha(archive.read(name)) != sha(raw):
                raise ValueError('Backup file checksum mismatch')
    return {'path': str(destination), 'sha256': sha(destination.read_bytes()),
            'profiles': len(people), 'references': len(photos), 'files': len(files),
            'scope': 'affected_catalog_cards_and_references_only'}


def write_report(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def run_import(engine, folder, root, output, *, apply=False, reference_limit=15,
               expected_count=None, embedding_fn=None, service_factory=None, skip_inactive=False,
               skip_over_limit=False):
    folder, root, output = Path(folder).resolve(), Path(root).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(folder):
        raise ValueError('Use a new output directory outside the input bundle')
    manifest = validate_bundle(folder, expected_count)
    planned = preflight(engine, manifest, reference_limit, skip_inactive, skip_over_limit)
    report = {'version': 1, 'mode': 'apply' if apply else 'dry_run', 'complete': False,
              'review_sha256': manifest['source_annotation_sha256'], 'started_at': datetime.now(timezone.utc).isoformat(),
              'face_processing_tested': False, 'backup': None, 'results': planned}
    output.mkdir(parents=True)
    report_path = output / 'report.json'
    def checkpoint():
        report['counts'] = dict(Counter(r['status'] for r in report['results']))
        write_report(report_path, report)
    checkpoint()
    if not apply or any(r['status'] == 'blocked' for r in planned):
        report['complete'] = True
        checkpoint()
        return report
    if not any(r['status'] == 'ready' for r in planned):
        report['complete'] = True
        checkpoint()
        return report
    lock = root / 'data/.reviewed-photo-import.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    # An exclusive file prevents two importers in the same deployment sharing the gallery.
    with lock.open('x', encoding='utf-8') as stream:
        stream.write(str(os.getpid()))
    try:
        planned = preflight(engine, manifest, reference_limit, skip_inactive, skip_over_limit)
        report['results'] = planned
        if any(r['status'] == 'blocked' for r in planned):
            report['complete'] = True
            checkpoint()
            return report
        report['backup'] = backup_references(engine, root, manifest, output / 'before-import.zip')
        for row in planned:
            if row['status'] == 'ready':
                row['status'] = 'not_attempted'
        checkpoint()
        if service_factory is None:
            from core.eduschool.photos import EduSchoolPhotoService
            service_factory = EduSchoolPhotoService
        service = service_factory(engine, root=root, reference_limit=reference_limit, migrate_schema=False)
        if embedding_fn is None:
            from core.ai.recognizer import FaceRecognizer
            recognizer = FaceRecognizer(use_cuda=False)
            embedding_fn = lambda image: recognizer.get_embedding(image, require_single=True)
        for item, result in zip(manifest['items'], planned):
            if result['status'] != 'not_attempted':
                continue
            raw = (folder / item['image']).read_bytes()
            if sha(raw) != item['sha256']:
                raise ValueError('Input photo changed after preflight')
            result['status'] = 'processing'
            checkpoint()
            try:
                service.add_local_photo(item['person_id'], raw, embedding_fn)
                from sqlalchemy import select
                from sqlalchemy.orm import Session
                from database.models import EduSchoolReferencePhoto as Photo
                with Session(engine) as session:
                    record = session.execute(select(Photo.id, Photo.photo_path).where(
                        Photo.person_id == item['person_id'], Photo.image_checksum == item['sha256'],
                        Photo.active.is_(True))).one()
                    result.update(status='imported', reference_id=record.id, photo_path=record.photo_path)
            except ValueError as error:
                result.update(status='rejected', reason=str(error))
            report['face_processing_tested'] = True
            checkpoint()
        report['complete'] = True
        checkpoint()
        return report
    except Exception as error:
        report['fatal_error_type'] = type(error).__name__
        checkpoint()
        raise
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-count', type=int, default=127)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--skip-inactive', action='store_true',
                        help='Leave out photos of profiles that are missing or inactive instead of blocking')
    parser.add_argument('--skip-over-limit', action='store_true',
                        help='Import photos in manifest order up to the per-person limit and leave out the rest')
    args = parser.parse_args()
    from database.manager import database_url
    from core.eduschool.catalog import load_settings
    engine = open_engine(database_url(), readonly=not args.apply)
    try:
        result = run_import(engine, args.bundle, ROOT, args.output, apply=args.apply, skip_inactive=args.skip_inactive,
                            skip_over_limit=args.skip_over_limit,
                            expected_count=args.expected_count,
                            reference_limit=load_settings().local_reference_photo_limit)
        print(json.dumps({'mode': result['mode'], 'complete': result['complete'], 'counts': result['counts'],
                          'report': str(args.output / 'report.json')}, ensure_ascii=True))
        if any(row['status'] in {'blocked', 'rejected', 'not_attempted', 'processing'} for row in result['results']):
            return 2
        return 0
    finally:
        engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
