import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

from core.catalog_transfer import CatalogError


def transfer_page():
    from types import SimpleNamespace
    from ui.catalog_transfer import render_catalog_transfer
    settings = SimpleNamespace(base_url="https://erp.test", local_reference_photo_limit=10)
    people = [SimpleNamespace(id="00000000-0000-0000-0000-000000000001", fio="Test Person")]
    render_catalog_transfer(None, settings, people)


class CatalogTransferUITests(unittest.TestCase):
    def setUp(self):
        self.service = MagicMock()
        self.service.export.return_value = (b"test archive", [])
        self.service.prepare.return_value = SimpleNamespace(recomputed=False, bundle=SimpleNamespace(warnings=[]))
        self.service.preview.return_value = {"new": 1, "fill": 0, "photos": 1, "state": "checked-state",
                                             "rows": [{"ERP ID": "one", "Действие": "Добавить"}]}
        self.service.apply.return_value = {"added": 1, "filled": 0, "references": 0}
        factory = patch("ui.catalog_transfer.CatalogTransfer", return_value=self.service)
        factory.start()
        self.addCleanup(factory.stop)
        self.app = AppTest.from_function(transfer_page, default_timeout=20)

    def upload(self):
        uploaded = io.BytesIO(b"test archive")
        uploaded.size = len(uploaded.getvalue())
        patcher = patch("ui.catalog_transfer.st.file_uploader", return_value=uploaded)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_export_produces_download_without_import(self):
        self.app.run()
        self.app.button(key="catalog_export").click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(self.app.success)
        self.assertEqual(len(self.app.get("download_button")), 1)
        self.service.apply.assert_not_called()

    def test_import_requires_preview_and_confirmation_then_shows_receipt(self):
        self.upload()
        self.app.run()
        self.assertFalse(any(b.key == "catalog_apply" for b in self.app.button))
        self.app.button(key="catalog_validate").click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(self.app.button(key="catalog_apply").disabled)
        self.service.apply.assert_not_called()
        confirmation = next(c for c in self.app.checkbox if c.key.startswith("catalog_confirm_"))
        confirmation.check().run()
        self.app.button(key="catalog_apply").click().run()
        self.assertFalse(self.app.exception)
        self.service.apply.assert_called_once_with(self.service.prepare.return_value, "checked-state", confirmed_same_center=True)
        self.assertIn("Импорт завершён", self.app.success[0].value)

    def test_invalid_archive_has_error_and_no_apply_button(self):
        self.upload()
        self.service.read.side_effect = CatalogError("Invalid catalog")
        self.app.run()
        self.app.button(key="catalog_validate").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.error[0].value, "Invalid catalog")
        self.assertFalse(any(b.key == "catalog_apply" for b in self.app.button))
        self.service.apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
