"""Guarded legacy-only cleanup; stop legacy sync and cameras before applying."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid
import zipfile

from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.models import (RemotePerson, RemotePersonReferencePhoto, Employee,
                             EduSchoolReferencePhoto, RecognitionEvent, UnknownVisitor)


def purge_legacy(engine, root, expected_people, backup, *, apply=False):
    root = Path(root).resolve()
    photo_root = root / 'data/persons'
    if photo_root.resolve() != photo_root:
        raise ValueError('Photo root must not be a symlink')
    if expected_people <= 0:
        raise ValueError('An explicit positive expected count is required')
    files = set()
    with Session(engine) as session, session.begin():
        if engine.dialect.name == 'postgresql' and apply:
            session.connection().exec_driver_sql(
                'LOCK TABLE remote_persons, remote_person_reference_photos IN EXCLUSIVE MODE')
        people = session.query(RemotePerson).all()
        if len(people) != expected_people:
            raise ValueError('Legacy count changed; no data deleted')
        refs = session.query(RemotePersonReferencePhoto).all()
        protected = set()
        for model, column in ((Employee, Employee.photo_path), (EduSchoolReferencePhoto, EduSchoolReferencePhoto.photo_path),
                              (RecognitionEvent, RecognitionEvent.photo_path), (UnknownVisitor, UnknownVisitor.primary_photo_path)):
            for (value,) in session.query(column).filter(column.isnot(None)):
                protected.add((root / value).resolve())
        allowed_folders = set()
        for person in people:
            identifier = str(uuid.UUID(person.id))
            main = photo_root / f'{identifier}.jpg'
            if person.photo_path and (root / person.photo_path).resolve() != main.resolve():
                raise ValueError('Unexpected legacy primary photo path; inspect manually')
            if main.exists():
                files.add(main)
            folder = photo_root / 'local' / hashlib.sha256(person.id.encode()).hexdigest()[:16]
            if folder.is_symlink():
                raise ValueError('Unsafe reference folder')
            allowed_folders.add(folder.resolve())
            if folder.exists():
                files.update(folder.rglob('*'))
        for photo in refs:
            path = root / photo.photo_path
            if path.resolve().parent not in allowed_folders:
                raise ValueError('Unexpected legacy reference path')
            if path.exists():
                files.add(path)
        for path in files:
            resolved = path.resolve()
            if (path.is_symlink() or not path.is_file() or not resolved.is_relative_to(photo_root.resolve())
                    or resolved in protected):
                raise ValueError('Photo is unsafe or referenced outside the legacy catalog; no data deleted')
        result = {'people': len(people), 'reference_rows': len(refs), 'photo_files': len(files)}
        if not apply:
            return result
        backup = Path(backup)
        if not backup.resolve().is_relative_to(root / 'data/backups'):
            raise ValueError('Use a private local data/backups destination')
        backup.parent.mkdir(parents=True, exist_ok=True)
        def record(row):
            return {column.name: getattr(row, column.name) for column in row.__table__.columns}
        manifest = {'version': 1, 'people': [record(row) for row in people],
                    'references': [record(row) for row in refs],
                    'files': {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}
        with zipfile.ZipFile(backup, 'x', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('legacy.json', json.dumps(manifest, default=str))
            for path in files:
                archive.write(path, str(path.relative_to(root)))
        with zipfile.ZipFile(backup) as archive:
            if archive.testzip() is not None:
                raise ValueError('Backup verification failed')
        session.query(RemotePersonReferencePhoto).delete(synchronize_session=False)
        removed = session.query(RemotePerson).delete(synchronize_session=False)
        if removed != expected_people:
            raise ValueError('Deletion count changed; transaction rolled back')
    # Only exact validated files are removed, never the shared persons directory.
    for path in files:
        path.unlink()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-people', type=int, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from core.edge.config import load_edge_settings
    from database.manager import get_engine
    if args.apply and load_edge_settings().enabled:
        raise SystemExit('Disable edge_integration.enabled and stop edge-sync before deletion')
    engine = get_engine()
    try:
        result = purge_legacy(engine, ROOT, args.expected_people, args.backup, apply=args.apply)
        print(json.dumps({'mode': 'deleted' if args.apply else 'dry_run', **result}))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
