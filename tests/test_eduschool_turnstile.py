import tempfile
import unittest
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from sqlalchemy import create_engine

from core.eduschool.turnstile import (
    DeliveryResult, EduSchoolTurnstileService, TurnstileSettings,
    reconcile_ambiguous, send_attendance, set_person_hold,
)
from database.models import (
    EduSchoolCatalogPerson, EduSchoolReferencePhoto, EduSchoolTurnstileOutbox,
    EduSchoolTurnstileState, RecognitionEvent,
)


BRANCH = "661d00dea4401645477617d9"
EXTERNAL_ID = "661d00dea440164547761701"
PERSON_ID = f"employee:{EXTERNAL_ID}"


class EduSchoolTurnstileTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.tempdir.name) / 'turnstile.db'}")
        self.settings = TurnstileSettings(enabled=True, branch_id=BRANCH, api_key="test-other-key",
                                          device_ids={"entry_cam": "GATE-ENTRY", "exit_cam": "GATE-EXIT"})
        self.service = EduSchoolTurnstileService(self.settings, self.engine)
        with self.service.Session.begin() as session:
            session.add(EduSchoolCatalogPerson(
                id=PERSON_ID, person_type="employee", external_id=EXTERNAL_ID,
                full_name="Staff One", source_status="active", active=True, employee_no="A-17",
            ))
            session.add(EduSchoolReferencePhoto(
                id="ref-photo", person_id=PERSON_ID, photo_path="data/ref.jpg", source="local",
                image_checksum="abc", embedding=[1.0] + [0.0] * 511, active=True,
            ))

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def event(self, suffix, *, person_id=f"edu:e:{EXTERNAL_ID}", person_type="eduschool_employee",
              camera_id="entry_cam", event_type="entry", created_at=None):
        record = RecognitionEvent(
            id=f"00000000-0000-0000-0000-{suffix:012d}", camera_id=camera_id,
            event_type=event_type, person_id=person_id, person_type=person_type,
            person_name="Staff One", subject_signature=f"subject-{suffix}",
            created_at=created_at or datetime.now(timezone.utc),
        )
        with self.service.Session.begin() as session:
            session.add(record)
        return record.id

    def outbox(self, event_id):
        with self.service.Session() as session:
            return session.get(EduSchoolTurnstileOutbox, event_id)

    def test_automatic_qualification_activation_and_payload(self):
        old = self.event(1)
        self.assertTrue(self.service.prepare_activation())
        self.assertEqual(self.service.queue_new_events(), 0)
        with self.assertRaisesRegex(ValueError, "Выберите сотрудника"):
            set_person_hold("student:missing", True, self.engine)
        self.assertEqual(self.service.refresh_auto_approvals(), 1)
        self.assertEqual(self.service.refresh_auto_approvals(), 0)
        entry = self.event(2)
        exit_event = self.event(3, camera_id="exit_cam", event_type="exit")
        local = self.event(4, person_id="local:4", person_type="local_employee")
        student = self.event(5, person_id=f"edu:s:{EXTERNAL_ID}", person_type="eduschool_student")
        self.assertEqual(self.service.queue_new_events(), 2)
        self.assertEqual(self.service.queue_new_events(), 0)
        self.assertIsNone(self.outbox(old))
        self.assertIsNone(self.outbox(local))
        self.assertIsNone(self.outbox(student))
        self.assertEqual(self.outbox(entry).payload["eventType"], "check_in")
        self.assertEqual(self.outbox(exit_event).payload["eventType"], "check_out")
        self.assertEqual(self.outbox(exit_event).payload["deviceId"], "GATE-EXIT")
        self.assertEqual(self.outbox(entry).payload["employeeNo"], "A-17")
        self.assertTrue(self.outbox(entry).payload["eventTime"].endswith("Z"))

    def test_missing_number_and_manual_hold_never_send(self):
        self.service.prepare_activation()
        with self.service.Session.begin() as session:
            session.get(EduSchoolCatalogPerson, PERSON_ID).employee_no = None
        self.assertEqual(self.service.refresh_auto_approvals(), 0)
        event = self.event(1)
        self.service.queue_new_events()
        self.assertEqual(self.outbox(event).status, "skipped")
        with self.service.Session.begin() as session:
            session.get(EduSchoolCatalogPerson, PERSON_ID).employee_no = "A-17"
        self.assertEqual(self.service.refresh_auto_approvals(), 1)
        current = self.event(2)
        self.service.queue_new_events()
        set_person_hold(PERSON_ID, True, self.engine)
        self.assertEqual(self.outbox(current).status, "blocked")
        self.assertEqual(self.service.refresh_auto_approvals(), 0)
        with self.service.Session() as session:
            self.assertFalse(session.get(EduSchoolCatalogPerson, PERSON_ID).attendance_approved)
        with patch("core.eduschool.turnstile.send_attendance") as sender:
            self.assertEqual(self.service.deliver_due(), 0)
            sender.assert_not_called()
        set_person_hold(PERSON_ID, False, self.engine)
        self.assertEqual(self.service.refresh_auto_approvals(), 1)
        self.assertEqual(self.outbox(current).status, "blocked")

    def test_duplicate_number_and_missing_photo_revoke_automatic_qualification(self):
        self.service.prepare_activation()
        self.assertEqual(self.service.refresh_auto_approvals(), 1)
        with self.service.Session.begin() as session:
            session.add(EduSchoolCatalogPerson(
                id="employee:661d00dea440164547761703", person_type="employee",
                external_id="661d00dea440164547761703", full_name="Other Staff",
                source_status="active", active=True, employee_no="A-17",
            ))
        self.assertEqual(self.service.refresh_auto_approvals(), 1)
        with self.service.Session() as session:
            self.assertFalse(session.get(EduSchoolCatalogPerson, PERSON_ID).attendance_approved)
        with self.service.Session.begin() as session:
            session.get(EduSchoolCatalogPerson, "employee:661d00dea440164547761703").active = False
        self.assertEqual(self.service.refresh_auto_approvals(), 1)
        with self.service.Session.begin() as session:
            session.get(EduSchoolReferencePhoto, "ref-photo").active = False
        self.assertEqual(self.service.refresh_auto_approvals(), 1)
        with self.service.Session() as session:
            self.assertFalse(session.get(EduSchoolCatalogPerson, PERSON_ID).attendance_approved)
        with self.service.Session.begin() as session:
            photo = session.get(EduSchoolReferencePhoto, "ref-photo")
            photo.active = True
            photo.embedding = [0.0] * 512
        self.assertEqual(self.service.refresh_auto_approvals(), 0)

    def test_hold_during_request_requires_reconciliation(self):
        self.service.prepare_activation()
        self.service.refresh_auto_approvals()
        event = self.event(1)
        self.service.queue_new_events()
        with patch("core.eduschool.turnstile.send_attendance", side_effect=lambda *_: (
            set_person_hold(PERSON_ID, True, self.engine), DeliveryResult("sent", code=0)
        )[1]):
            self.assertEqual(self.service.deliver_due(), 0)
        self.assertEqual(self.outbox(event).status, "ambiguous")

    def test_network_retry_duplicate_success_and_restart(self):
        self.service.prepare_activation()
        self.service.refresh_auto_approvals()
        event = self.event(1)
        self.service.queue_new_events()
        with patch("core.eduschool.turnstile.send_attendance", side_effect=[
            DeliveryResult("retry", reason="HTTP 503"),
            DeliveryResult("sent", code=0, backend_id="backend-1", duplicate=True),
        ]) as sender:
            self.assertEqual(self.service.deliver_due(), 0)
            self.assertEqual(self.outbox(event).status, "retry")
            self.assertEqual(self.service.deliver_due(), 0)
            with self.service.Session.begin() as session:
                session.get(EduSchoolTurnstileOutbox, event).next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            self.assertEqual(self.service.deliver_due(), 1)
            self.assertEqual(sender.call_count, 2)
        self.assertEqual(self.outbox(event).status, "sent")
        self.assertTrue(self.outbox(event).duplicate)
        self.assertTrue(EduSchoolTurnstileService(self.settings, self.engine).prepare_activation())
        self.assertEqual(self.service.queue_new_events(), 0)

    def test_timeout_and_interrupted_request_are_ambiguous(self):
        self.service.prepare_activation()
        self.service.refresh_auto_approvals()
        event = self.event(1)
        self.service.queue_new_events()
        with patch("core.eduschool.turnstile.send_attendance", return_value=DeliveryResult("ambiguous")):
            self.assertEqual(self.service.deliver_due(), 0)
        self.assertEqual(self.outbox(event).status, "ambiguous")
        with self.service.Session.begin() as session:
            session.get(EduSchoolTurnstileOutbox, event).status = "sending"
        self.service.prepare_activation()
        self.assertEqual(self.outbox(event).status, "ambiguous")
        self.assertEqual(self.service.deliver_due(), 0)
        reconcile_ambiguous(event, already_delivered=True, engine=self.engine)
        self.assertEqual(self.outbox(event).status, "sent")
        with self.assertRaises(ValueError):
            reconcile_ambiguous(event, already_delivered=False, engine=self.engine)

    def test_manual_retry_requires_reconciliation_and_current_qualification(self):
        self.service.prepare_activation()
        self.service.refresh_auto_approvals()
        event = self.event(1)
        self.service.queue_new_events()
        with self.service.Session.begin() as session:
            session.get(EduSchoolTurnstileOutbox, event).status = "ambiguous"
        reconcile_ambiguous(event, already_delivered=False, engine=self.engine)
        self.assertEqual(self.outbox(event).status, "retry")
        set_person_hold(PERSON_ID, True, self.engine)
        with patch("core.eduschool.turnstile.send_attendance") as sender:
            self.assertEqual(self.service.deliver_due(), 0)
            sender.assert_not_called()

    def test_terminal_error_and_disable_block_queue(self):
        self.service.prepare_activation()
        self.service.refresh_auto_approvals()
        event = self.event(1)
        self.service.queue_new_events()
        with patch("core.eduschool.turnstile.send_attendance", return_value=DeliveryResult("blocked", code=55103)):
            self.service.deliver_due()
        self.assertEqual(self.outbox(event).response_code, 55103)
        self.assertEqual(self.outbox(event).status, "blocked")
        disabled = EduSchoolTurnstileService(TurnstileSettings(), self.engine)
        self.assertFalse(disabled.prepare_activation())
        with disabled.Session() as session:
            self.assertFalse(session.get(EduSchoolTurnstileState, 1).enabled)

    def test_http_envelopes_and_transport_errors(self):
        payload = {"employeeNo": "A-17", "eventType": "check_in"}
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _): return b'{"code":0,"data":{"_id":"abc","duplicate":true}}'
        class Opener:
            def open(self, request, timeout):
                self.request = request
                return Response()
        opener = Opener()
        with patch("core.eduschool.turnstile.build_opener", return_value=opener):
            result = send_attendance(self.settings, payload)
        self.assertEqual(result.status, "sent")
        self.assertTrue(result.duplicate)
        self.assertEqual(opener.request.get_header("Apikey"), "test-other-key")
        class ErrorOpener:
            def __init__(self, error): self.error = error
            def open(self, *_args, **_kwargs): raise self.error
        for code, expected in ((55103, "blocked"), (10600, "blocked"), (10004, "blocked"), (0, "retry")):
            status = 503 if code == 0 else 400
            error = HTTPError("https://example.test", status, "bad", {}, None)
            error.read = lambda _: ('{"code":%d}' % code).encode()
            with patch("core.eduschool.turnstile.build_opener", return_value=ErrorOpener(error)):
                self.assertEqual(send_attendance(self.settings, payload).status, expected)
        with patch("core.eduschool.turnstile.build_opener", return_value=ErrorOpener(URLError(TimeoutError()))):
            self.assertEqual(send_attendance(self.settings, payload).status, "ambiguous")
        with patch("core.eduschool.turnstile.build_opener", return_value=ErrorOpener(URLError(socket.gaierror()))):
            self.assertEqual(send_attendance(self.settings, payload).status, "retry")
        with patch("core.eduschool.turnstile.build_opener", return_value=ErrorOpener(URLError(ConnectionResetError()))):
            self.assertEqual(send_attendance(self.settings, payload).status, "ambiguous")


if __name__ == "__main__":
    unittest.main()
