import tempfile
import unittest
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from core.camera_controls import merge_device_ids, read_controls, save_camera
from core.config import CameraSettings, ConfigurationError, load_app_settings
from core.eduschool.turnstile import load_settings
from core.eduschool.turnstile_worker import refresh_camera_routes


class CameraOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.controls = root / 'camera_controls.json'
        self.config = root / 'settings.yaml'
        self.original = (
            'database:\n  path: data/test.db\ncameras:\n'
            '  - id: entry\n    name: Entry\n    rtsp_url: rtsp://camera/1\n'
            'eduschool_turnstile:\n  enabled: true\n'
            '  branch_id: 661d00dea4401645477617d9\n'
            '  device_ids:\n    entry: original-device\n'
        )
        self.config.write_text(self.original)

    def settings(self):
        return load_settings(self.config, controls_path=self.controls)

    def save(self, camera):
        save_camera(camera, expected_revision=read_controls(self.controls)[1], path=self.controls,
                    base_device_ids=self.settings().device_ids)

    def camera(self, name='entry_02', **kwargs):
        return CameraSettings(name, 'rtsp://user:secret@camera/2', name='New camera', device_id=name, **kwargs)

    def test_routes_reload_without_changing_activation_or_credentials(self):
        before = self.settings()
        service = SimpleNamespace(settings=replace(before, api_key='original-key'))
        for direction in ('entry', 'exit'):
            self.save(self.camera(direction + '_02', event_type=direction))
        with patch('core.eduschool.turnstile_worker.load_settings', return_value=self.settings()):
            refresh_camera_routes(service)
        self.assertEqual(service.settings.device_ids,
                         {'entry': 'original-device', 'entry_02': 'entry_02', 'exit_02': 'exit_02'})
        self.assertEqual(replace(service.settings, device_ids=before.device_ids),
                         replace(before, api_key='original-key'))
        self.assertEqual(self.config.read_text(), self.original)
        self.assertEqual(len(load_app_settings(self.config, controls_path=self.controls).cameras), 3)

    def test_disabled_camera_keeps_route_for_pending_events(self):
        camera = self.camera()
        self.save(camera)
        self.save(replace(camera, is_active=False))
        self.assertEqual(self.settings().device_ids['entry_02'], 'entry_02')

    def test_duplicate_and_reassigned_routes_are_rejected_without_writing(self):
        camera = self.camera()
        self.save(camera)
        before = self.controls.read_bytes()
        for invalid in (replace(camera, id='other'), replace(camera, device_id='changed'),
                        replace(camera, device_id=''), replace(camera, id='entry', device_id='changed'),
                        replace(camera, id='other', device_id='original-device')):
            with self.assertRaises(ConfigurationError):
                self.save(invalid)
            self.assertEqual(self.controls.read_bytes(), before)

    def test_invalid_device_never_saved(self):
        for value in ('x' * 65, ' space ', None):
            with self.assertRaises(ConfigurationError):
                self.save(replace(self.camera(), device_id=value))
        self.assertFalse(self.controls.exists())

    def test_old_controls_keep_yaml_routes(self):
        self.save(replace(self.camera('entry'), device_id=''))
        self.assertEqual(self.settings().device_ids, {'entry': 'original-device'})

    def test_corrupt_routes_do_not_replace_sender_settings(self):
        service = SimpleNamespace(settings=self.settings())
        before = service.settings
        self.controls.write_text('{broken')
        with patch('core.eduschool.turnstile_worker.load_settings', side_effect=self.settings):
            with self.assertRaises(ValueError):
                refresh_camera_routes(service)
        self.assertIs(service.settings, before)

    def test_unrelated_config_ignores_device_controls(self):
        self.save(self.camera())
        self.assertEqual(load_settings(self.config).device_ids, {'entry': 'original-device'})
        with self.assertRaises(ConfigurationError):
            merge_device_ids({'entry': 'original-device'}, {'bad': {'id': 'different'}})

    def test_editor_adds_exit_preserves_secret_and_can_edit_twice(self):
        def page():
            from ui.cameras import render_cameras
            render_cameras()

        with patch('ui.cameras.render_runtime_panel'), \
             patch('ui.cameras.load_app_settings', side_effect=lambda: load_app_settings(self.config, controls_path=self.controls)), \
             patch('ui.cameras.load_turnstile_settings', side_effect=self.settings), \
             patch('ui.cameras.read_controls', side_effect=partial(read_controls, self.controls)), \
             patch('ui.cameras.save_camera', side_effect=partial(save_camera, path=self.controls)), \
             patch('ui.cameras.preview_path', return_value=Path(self.directory.name) / 'missing.jpg'):
            app = AppTest.from_function(page, default_timeout=15).run()
            self.assertFalse(app.exception)
            app.button[0].click().run()
            fields = {item.label: item for item in app.text_input}
            fields['ID камеры'].set_value('exit_02')
            fields['Название'].set_value('Exit two')
            fields['Адрес потока RTSP'].set_value('rtsp://user:secret@camera/2')
            next(item for item in app.selectbox if item.label == 'Направление').set_value('exit')
            app.toggle[0].set_value(True)
            next(item for item in app.button if item.label == 'Сохранить камеру').click().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.success)
            fields = {item.label: item for item in app.text_input}
            self.assertEqual(fields['Адрес потока RTSP'].value, '')
            self.assertTrue(fields['deviceId API'].disabled)
            self.assertTrue(fields['ID камеры'].disabled)
            fields['Название'].set_value('Updated exit')
            app.toggle[0].set_value(False)
            next(item for item in app.button if item.label == 'Сохранить камеру').click().run()
            self.assertFalse(app.exception)
            saved = read_controls(self.controls)[0]['exit_02']
            self.assertFalse(saved['is_active'])
            self.assertEqual(saved['rtsp_url'], 'rtsp://user:secret@camera/2')
            self.assertEqual(saved['device_id'], 'exit_02')
            self.assertEqual(saved['event_type'], 'exit')
            self.assertEqual(saved['name'], 'Updated exit')


if __name__ == '__main__':
    unittest.main()
