"""Destructive-to-temporary-config UAT: only runs with dedicated tmpfs mounts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from dataclasses import replace

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.camera_controls import read_controls, save_camera
from core.config import CameraSettings


def main():
    mounts = Path('/proc/mounts').read_text().splitlines()
    for target in ('/app/data', '/app/config'):
        if not any(row.split()[1:3] == [target, 'tmpfs'] for row in mounts):
            raise SystemExit('Refusing UAT: data and config must be separate tmpfs mounts')
    source = yaml.safe_load(Path('/source-config/settings.yaml').read_text())
    cameras = source['cameras'][:2]
    if len(cameras) != 2:
        raise SystemExit('Two configured camera sources are required')
    config = {
        'database': {'path': 'data/uat.db'},
        'cameras': [{**camera, 'is_active': True, 'profile': 'balanced'} for camera in cameras[:1]],
        'ai': {'face_detection_imgsz': 640, 'face_detection_fps': 12},
        'edge_integration': {'enabled': False}, 'eduschool_catalog': {'enabled': False},
        'eduschool_turnstile': {'enabled': False}, 'health': {'enabled': False},
        'unknown_visitors': {'enabled': False},
    }
    Path('/app/config/settings.yaml').write_text(yaml.safe_dump(config))
    os.environ.update(VISION_OFFICE_DATABASE_URL='sqlite:////app/data/uat.db',
                      VISION_OFFICE_EXTERNAL_EDGE_SYNC='true', VISION_OFFICE_HEADLESS='true')
    from core.performance import configured_camera_statuses, read_runtime_status
    from core.config import load_app_settings

    def wait_for(predicate, label, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Supervisor exited unexpectedly; inspect isolated log')
            states = {item['camera_id']: item for item in configured_camera_statuses()}
            if predicate(states):
                print(json.dumps({'check': label, 'passed': True}), flush=True)
                return states
            time.sleep(1)
        raise RuntimeError('Timed out: ' + label)

    def ready(item):
        return item.get('ai_ready') and item.get('stream_status') == 'connected' and item.get('detection_fps', 0) > 0

    def save(camera):
        save_camera(camera, expected_revision=read_controls()[1])

    log = Path('/tmp/camera-uat.log').open('w')
    process = subprocess.Popen([sys.executable, 'main.py'], stdout=log, stderr=log)
    try:
        first = load_app_settings().cameras[0]
        wait_for(lambda states: ready(states[first.id]), 'first camera connected with AI')
        second = CameraSettings(id=cameras[1]['id'], rtsp_url=cameras[1]['rtsp_url'],
                                name='UAT second camera', event_type='exit', profile='balanced')
        save(second)
        states = wait_for(lambda states: len(states) == 2 and all(ready(item) for item in states.values()), 'add second camera live')
        print(json.dumps({'metrics': [{key: item.get(key) for key in (
            'camera_id', 'capture_fps', 'detection_fps', 'yolo_device', 'face_device', 'frame_age_ms'
        )} for item in states.values()]}), flush=True)
        save(replace(second, is_active=False))
        wait_for(lambda states: ready(states[first.id]) and any(
            item.get('camera_id') == second.id and item.get('stream_status') == 'stopped'
            and not item.get('running') for item in (read_runtime_status() or {}).get('cameras', [])
        ), 'pause second camera without stopping first')
        save(second)
        wait_for(lambda states: all(ready(item) for item in states.values()), 'resume second camera')
    finally:
        process.terminate()
        try:
            process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise RuntimeError('Supervisor shutdown exceeded 40 seconds')
        log.close()
        print(json.dumps({'check': 'supervisor stopped', 'exit_code': process.returncode}), flush=True)


if __name__ == '__main__':
    main()
