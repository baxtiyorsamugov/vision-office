import unittest
from dataclasses import replace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.eduschool.delivery_history import delivery_history_query
from core.eduschool.turnstile import TurnstileSettings
from database.models import Base, EduSchoolDeliveryAttempt, EduSchoolTurnstileOutbox, RecognitionEvent


class DeliveryHistoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.addCleanup(self.session.close)

    def test_legacy_result_remains_visible_without_http_or_catalog_or_event(self):
        self.session.add(EduSchoolTurnstileOutbox(event_id="legacy", person_id="employee:old",
                         status="sent", attempts=1, response_code=0, backend_event_id="server-record"))
        self.session.commit()
        rows = delivery_history_query(self.session).all()
        self.assertEqual(len(rows), 1)
        item, event, person, attempt = rows[0]
        self.assertEqual(item.backend_event_id, "server-record")
        self.assertEqual(item.response_code, 0)
        self.assertIsNone(attempt)
        self.assertIsNone(event)
        self.assertIsNone(person)
        self.assertEqual(self.session.query(EduSchoolDeliveryAttempt).count(), 0)

    def test_many_attempts_show_one_event_with_latest_http_result(self):
        self.session.add(RecognitionEvent(id="new", camera_id="entry", event_type="entry", subject_signature="test"))
        self.session.add(EduSchoolTurnstileOutbox(event_id="new", person_id="student:new", status="sent", attempts=2))
        for number, status in [(1, "retry"), (2, "sent")]:
            self.session.add(EduSchoolDeliveryAttempt(id=str(number), event_id="new", attempt_number=number,
                             endpoint="https://example.test/attendance", status=status, http_status=503 if number == 1 else 200))
        self.session.commit()
        query = delivery_history_query(self.session)
        self.assertEqual(query.count(), 1)
        self.assertEqual(query.one()[3].http_status, 200)
        self.assertEqual(self.session.query(EduSchoolDeliveryAttempt).count(), 2)

    def test_ui_can_validate_routes_without_exposing_or_requiring_sender_key(self):
        settings = TurnstileSettings(enabled=True, branch_id="661a6d45c518a731ebbbab77",
                                     device_ids={"entry": "GATE-ENTRY-1"})
        self.assertEqual(settings.validation_error(), "EDUSCHOOL_TURNSTILE_API_KEY is missing")
        self.assertIsNone(settings.validation_error(check_credentials=False))
        self.assertIn("branch_id", replace(settings, branch_id="bad").validation_error(check_credentials=False))


if __name__ == "__main__":
    unittest.main()
