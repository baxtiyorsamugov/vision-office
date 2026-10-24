"""Derive a separate unknown-only human review using existing local media."""
import argparse
import copy
import json
import os
from pathlib import Path

from build_photo_review import digest, render, snapshot_id


def build(source, output):
    source, output = source.resolve(), output.resolve()
    if output == source or output.is_relative_to(source):
        raise ValueError('Use a separate output directory')
    protected = [source / 'manifest.json', source / 'index.html']
    before = {p.name: digest(p) for p in protected}
    original = json.loads(protected[0].read_text(encoding='utf-8'))
    if original['snapshot_id'] != snapshot_id(original):
        raise ValueError('Source snapshot integrity mismatch')
    target = output / 'manifest.json'
    if target.exists():
        report = json.loads(target.read_text(encoding='utf-8'))
        if (report.get('source_manifest_sha256') != before['manifest.json']
                or report.get('review_scope') != 'unknown'
                or report['snapshot_id'] != snapshot_id(report)):
            raise ValueError('Existing output belongs to a different snapshot')
    else:
        if output.exists():
            raise ValueError('Output directory already exists without a manifest')
        report = copy.deepcopy(original)
        report['events'] = [e for e in report['events'] if e['prediction_type'] == 'unknown']
        if not report['events']:
            raise ValueError('No unknown frames in this source')
        report['review_scope'] = 'unknown'
        report['source_manifest_sha256'] = before['manifest.json']
        report['parent_snapshot_id'] = original['snapshot_id']
        rows = report['events'] + [r for p in report['people'] for r in p['references']]
        for row in rows:
            photo = (source / row['image']).resolve()
            if not photo.is_relative_to((source / 'media').resolve()) or digest(photo) != row['sha256']:
                raise ValueError('Photo path or checksum mismatch')
            row['image'] = Path(os.path.relpath(photo, output)).as_posix()
        report['snapshot_id'] = snapshot_id(report)
        if report['snapshot_id'] == original['snapshot_id']:
            raise ValueError('Separate storage identity required')
        output.mkdir(parents=True)
        target.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    render(output)
    if before != {p.name: digest(p) for p in protected}:
        raise ValueError('Source review changed during build')
    (output / 'source-preservation.json').write_text(json.dumps({
        'source': str(source), 'source_hashes': before, 'source_unchanged': True,
        'separate_annotation_storage': True, 'shared_media_read_only': True,
        'unknown_frames': len(report['events'])
    }, indent=2), encoding='utf-8')
    print(json.dumps({'page': str(output / 'index.html'), 'unknown_frames': len(report['events']),
                      'snapshot_id': report['snapshot_id'], 'source_unchanged': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.source, args.output)
