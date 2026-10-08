import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from streamlit.testing.v1 import AppTest

from core.edge.config import EdgeSettings
from database.models import Base


class LegacyRetirementUITests(unittest.TestCase):
    def test_eduschool_tabs_survive_disabled_legacy(self):
        engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        try:
            with patch('database.manager.get_engine', return_value=engine), \
                 patch('core.edge.config.load_edge_settings', return_value=EdgeSettings(enabled=False)), \
                 patch('core.eduschool.catalog.load_settings', return_value=SimpleNamespace(enabled=True)):
                app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'ui/app.py'), default_timeout=30).run()
                self.assertFalse(app.exception)
                app.radio[0].set_value('Сотрудники').run()
                self.assertFalse(app.exception)
                labels = [tab.label for tab in app.tabs]
                self.assertTrue(any(label.startswith('EduSchool') for label in labels))
                self.assertTrue(any(label.startswith('Неизвестные') for label in labels))
                self.assertTrue(any(label.startswith('Локальная база') for label in labels))
        finally:
            engine.dispose()


if __name__ == '__main__':
    unittest.main()
