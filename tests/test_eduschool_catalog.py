import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine

from core.eduschool.catalog import EduSchoolCatalogSettings, EduSchoolCatalogSync
from database.models import EduSchoolCatalogPerson, EduSchoolCatalogSyncState, RemotePerson


BRANCH = "661d00dea4401645477617d9"


class EduSchoolCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.tempdir.name) / 'catalog.db'}")
        self.settings = EduSchoolCatalogSettings(enabled=True, branch_id=BRANCH, bearer_token="test", api_key="test", page_size=1)
        self.service = EduSchoolCatalogSync(self.settings, self.engine)
        self.student = {
            "_id": "661d00dea440164547761701", "branchId": BRANCH,
            "fullName": "Student One", "status": {"state": "active"}, "imageUrl": "https://images.test/student.jpg",
        }
        self.employee = {
            "_id": "661d00dea440164547761702", "branchEmployee": {"branchId": BRANCH, "isActive": True},
            "fullName": "Employee One", "state": "active", "imageUrl": None,
        }

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _fake_pages(self, people):
        def get_page(kind, page):
            rows = people[kind]
            offset = (page - 1) * self.settings.page_size
            return {"data": {"total": len(rows), "data": rows[offset:offset + self.settings.page_size]}}
        self.service._fetch_page = get_page

    def test_paginated_sync_repeat_and_inactive_reconciliation(self):
        second = dict(self.student, _id="661d00dea440164547761703", fullName="Student Two", status={"state": "new"})
        self._fake_pages({"student": [self.student, second], "employee": [self.employee]})
        self.assertEqual(self.service.sync_once(), {"students": 2, "employees": 1})
        with self.service.Session.begin() as session:
            session.get(EduSchoolCatalogPerson, f"employee:{self.employee['_id']}").source_photo_status = "pending"
        self.assertEqual(self.service.sync_once(), {"students": 2, "employees": 1})
        with self.service.Session() as session:
            self.assertEqual(session.query(EduSchoolCatalogPerson).count(), 3)
            self.assertFalse(session.get(EduSchoolCatalogPerson, f"student:{second['_id']}").active)
            self.assertEqual(session.get(EduSchoolCatalogPerson, f"employee:{self.employee['_id']}").source_photo_status, "missing")
        self._fake_pages({"student": [self.student], "employee": [self.employee]})
        self.service.sync_once()
        with self.service.Session() as session:
            old = session.get(EduSchoolCatalogPerson, f"student:{second['_id']}")
            self.assertFalse(old.active)
            self.assertEqual(old.source_status, "absent")
            self.assertEqual(session.get(EduSchoolCatalogSyncState, 1).student_count, 1)

    def test_failed_second_catalog_preserves_old_snapshot_and_face_cache(self):
        self._fake_pages({"student": [self.student], "employee": [self.employee]})
        self.service.sync_once()
        with self.service.Session.begin() as session:
            session.add(RemotePerson(id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", person_type="employee", fio="Face profile", active=True))
        self._fake_pages({"student": [dict(self.student, fullName="Changed")], "employee": []})
        self.service._fetch_page = lambda kind, page: (_ for _ in ()).throw(ValueError("ERP offline")) if kind == "employee" else {"data": {"total": 1, "data": [dict(self.student, fullName="Changed")]}}
        with self.assertRaisesRegex(ValueError, "ERP offline"):
            self.service.sync_once()
        with self.service.Session() as session:
            self.assertEqual(session.get(EduSchoolCatalogPerson, f"student:{self.student['_id']}").full_name, "Student One")
            self.assertEqual(session.get(RemotePerson, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa").fio, "Face profile")
            self.assertIn("ERP offline", session.get(EduSchoolCatalogSyncState, 1).last_error)

    def test_wrong_branch_is_rejected_without_deactivation(self):
        self._fake_pages({"student": [self.student], "employee": [self.employee]})
        self.service.sync_once()
        self._fake_pages({"student": [dict(self.student, branchId="other")], "employee": [self.employee]})
        with self.assertRaisesRegex(ValueError, "outside configured branch"):
            self.service.sync_once()
        with self.service.Session() as session:
            self.assertTrue(session.get(EduSchoolCatalogPerson, f"student:{self.student['_id']}").active)

    def test_identical_duplicate_employee_rows_are_collapsed(self):
        self._fake_pages({"student": [], "employee": [self.employee, dict(self.employee)]})
        self.assertEqual(self.service.sync_once(), {"students": 0, "employees": 1})
        with self.service.Session() as session:
            self.assertEqual(session.query(EduSchoolCatalogPerson).count(), 1)
            self.assertEqual(session.get(EduSchoolCatalogSyncState, 1).employee_count, 1)

    def test_conflicting_duplicate_employee_rows_preserve_previous_snapshot(self):
        self._fake_pages({"student": [self.student], "employee": [self.employee]})
        self.service.sync_once()
        changed_employee = dict(self.employee, fullName="Different Employee")
        changed_student = dict(self.student, fullName="Changed Student")
        self._fake_pages({"student": [changed_student], "employee": [self.employee, changed_employee]})
        with self.assertRaisesRegex(ValueError, "conflicting duplicate ID"):
            self.service.sync_once()
        with self.service.Session() as session:
            self.assertEqual(session.get(EduSchoolCatalogPerson, f"student:{self.student['_id']}").full_name, "Student One")
            self.assertEqual(session.get(EduSchoolCatalogSyncState, 1).employee_count, 1)

    def test_empty_page_before_reported_total_is_rejected(self):
        self.service._fetch_page = lambda kind, page: {"data": {"total": 2, "data": []}}
        with self.assertRaisesRegex(ValueError, "incomplete page"):
            self.service.sync_once()

    def test_employee_number_and_source_photo_changes_revoke_approval(self):
        employee = dict(self.employee, employeeNo="A-17")
        self._fake_pages({"student": [], "employee": [employee]})
        self.service.sync_once()
        person_id = f"employee:{self.employee['_id']}"
        with self.service.Session.begin() as session:
            person = session.get(EduSchoolCatalogPerson, person_id)
            person.attendance_approved = True
            person.attendance_approved_at = datetime.now(timezone.utc)
        self.service.sync_once()
        with self.service.Session() as session:
            self.assertTrue(session.get(EduSchoolCatalogPerson, person_id).attendance_approved)
        self._fake_pages({"student": [], "employee": [dict(employee, employeeNo="A-18")]})
        self.service.sync_once()
        with self.service.Session() as session:
            person = session.get(EduSchoolCatalogPerson, person_id)
            self.assertEqual(person.employee_no, "A-18")
            self.assertFalse(person.attendance_approved)
        with self.service.Session.begin() as session:
            person = session.get(EduSchoolCatalogPerson, person_id)
            person.attendance_approved = True
            person.attendance_approved_at = datetime.now(timezone.utc)
        self._fake_pages({"student": [], "employee": [dict(employee, employeeNo="A-18", imageUrl="https://images.test/new.jpg")]})
        self.service.sync_once()
        with self.service.Session() as session:
            self.assertFalse(session.get(EduSchoolCatalogPerson, person_id).attendance_approved)


if __name__ == "__main__":
    unittest.main()
