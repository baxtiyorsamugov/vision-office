import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from sqlalchemy import create_engine

from core.edge.config import EdgeSettings
from database.models import RemotePerson, AccessLogOutbox
from tools import import_erp_catalog as catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = catalog.CatalogService(EdgeSettings(), create_engine('sqlite://'))
        self.person = {'id': '00000000-0000-0000-0000-000000000001',
                       'person_type': 'employee', 'fio': 'Example', 'photo_url': 'https://example.test/photo'}
        self.device = [{**self.person, 'embedding': [0.1] * 512}]
        self.root_patch = patch.object(catalog, 'ROOT', self.root)
        self.root_patch.start()
        self.sleep_patch = patch.object(catalog.time, 'sleep')
        self.sleep_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.sleep_patch.stop()
        self.service.engine.dispose()
        self.temp.cleanup()

    def test_repeat_import_preserves_vectors_downloads_portrait_and_never_queues_erp(self):
        portrait = self.root / 'portrait.jpg'
        def download(*args):
            portrait.touch()
            return np.zeros((2, 2, 3)), 'portrait.jpg'
        with patch.object(self.service, '_download_reference_photo', side_effect=download) as fetch:
            self.assertEqual(catalog.import_catalog(self.service, [self.person], self.device), 0)
            self.assertEqual(catalog.import_catalog(self.service, [self.person], self.device), 0)
            self.assertEqual(fetch.call_count, 1)
        with self.service.Session() as session:
            self.assertEqual(session.query(RemotePerson).count(), 1)
            self.assertEqual(session.query(RemotePerson).one().fio, 'Example')
            self.assertEqual(session.query(AccessLogOutbox).count(), 0)

    def test_wrong_center_does_not_write(self):
        with self.assertRaises(ValueError):
            catalog.import_catalog(self.service, [self.person], [])
        with self.service.Session() as session:
            self.assertEqual(session.query(RemotePerson).count(), 0)

    def test_missing_faceid_is_not_reported_as_success(self):
        person = {**self.person, 'photo_url': None}
        self.assertEqual(catalog.import_catalog(self.service, [person], [person]), 2)

    def test_external_photo_never_receives_admin_token(self):
        self.service.token = 'secret'
        with patch.object(catalog.requests, 'get') as fetch:
            response = fetch.return_value.__enter__.return_value
            response.is_redirect = False
            response.status_code = 403
            with self.assertRaises(ValueError):
                self.service._download_reference_photo(self.person['id'], 'https://other.test/photo')
            self.assertEqual(fetch.call_args.kwargs['headers'], {})


if __name__ == '__main__':
    unittest.main()
