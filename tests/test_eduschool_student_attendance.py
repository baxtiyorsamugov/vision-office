import unittest
import uuid
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DataError

from core.eduschool.turnstile import DeliveryResult, EduSchoolTurnstileService, TurnstileSettings, set_person_hold
from database.migrations import run_migrations
from database.models import (EduSchoolCatalogPerson, EduSchoolReferencePhoto, EduSchoolTurnstileOutbox,
                             EduSchoolTurnstileState, RecognitionEvent, EduSchoolDeliveryAttempt)


BRANCH = '661d00dea4401645477617d9'
STUDENT = '661d00dea440164547761701'
STAFF = '661d00dea440164547761702'


class StudentAttendanceTests(unittest.TestCase):
    def make_engine(self):
        return create_engine('sqlite://')

    def setUp(self):
        self.engine = self.make_engine()
        self.addCleanup(self.engine.dispose)
        self.settings = TurnstileSettings(enabled=True, students_enabled=True, branch_id=BRANCH,
                                          api_key='test-key', device_ids={'in': 'IN', 'out': 'OUT'})
        self.service = EduSchoolTurnstileService(self.settings, self.engine)
        self.person('student', STUDENT)
        self.person('employee', STAFF)

    def person(self, kind, external_id, number='15', branch=BRANCH, photo=True):
        with self.service.Session.begin() as session:
            session.add(EduSchoolCatalogPerson(
                id=f'{kind}:{external_id}', external_id=external_id, person_type=kind,
                full_name=f'Test {kind}', source_status='active', active=True, source_branch_id=branch,
                student_no=number if kind == 'student' else None,
                employee_no=number if kind == 'employee' else None))
            if photo:
                session.add(EduSchoolReferencePhoto(
                    id=str(uuid.uuid4()), person_id=f'{kind}:{external_id}', photo_path='data/test.jpg',
                    image_checksum=external_id, embedding=[1.0] + [0.0] * 511, active=True))

    def start(self, students=True):
        self.service.settings = replace(self.settings, students_enabled=students)
        self.assertTrue(self.service.prepare_activation())
        self.service.refresh_auto_approvals()

    def event(self, kind='student', external_id=STUDENT, direction='entry', when=None):
        event_id = str(uuid.uuid4())
        with self.service.Session.begin() as session:
            session.add(RecognitionEvent(
                id=event_id, camera_id='in' if direction == 'entry' else 'out', event_type=direction,
                person_id=f"edu:{'s' if kind == 'student' else 'e'}:{external_id}",
                person_type=f'eduschool_{kind}', subject_signature=external_id,
                created_at=when or datetime.now(timezone.utc)))
        return event_id

    def item(self, event_id):
        with self.service.Session() as session:
            return session.get(EduSchoolTurnstileOutbox, event_id)

    def test_staff_and_student_same_number_are_distinct_and_send_one_identifier(self):
        self.start()
        events = [self.event(), self.event(direction='exit'), self.event('employee', STAFF)]
        self.assertEqual(self.service.queue_new_events(), 3)
        with patch('core.eduschool.turnstile.send_attendance', return_value=DeliveryResult('sent', code=0, http_status=200)) as sender:
            self.assertEqual(self.service.deliver_due(), 3)
        payloads = [call.args[1] for call in sender.call_args_list]
        self.assertEqual([(p.get('studentNo'), p.get('employeeNo'), p['eventType']) for p in payloads],
                         [('15', None, 'check_in'), ('15', None, 'check_out'), (None, '15', 'check_in')])
        self.assertTrue(all(p['isCamera'] is True for p in payloads))
        with self.service.Session() as session:
            self.assertEqual(session.query(EduSchoolDeliveryAttempt).filter_by(http_status=200).count(), 3)
        self.assertTrue(all(self.item(event).status == 'sent' for event in events))
        self.assertEqual(self.service.queue_new_events(), 0)

    def test_disabled_students_and_old_history_are_not_enqueued(self):
        self.start(students=False)
        old = self.event()
        self.assertEqual(self.service.queue_new_events(), 0)
        self.start(students=True)
        before_activation = self.event(when=datetime.now(timezone.utc) - timedelta(days=1))
        new = self.event()
        self.assertEqual(self.service.queue_new_events(), 1)
        self.assertIsNone(self.item(old))
        self.assertIsNone(self.item(before_activation))
        self.assertEqual(self.item(new).status, 'pending')

    def test_restart_preserves_student_activation_and_pending_queue(self):
        self.start()
        event_id = self.event()
        self.service.queue_new_events()
        with self.service.Session() as session:
            activated = session.get(EduSchoolTurnstileState, 1).students_activated_at
        self.start()
        with self.service.Session() as session:
            self.assertEqual(session.get(EduSchoolTurnstileState, 1).students_activated_at, activated)
        self.assertEqual(self.item(event_id).status, 'pending')

    def test_disabling_blocks_pending_and_reenable_does_not_replay(self):
        self.start()
        event_id = self.event()
        self.service.queue_new_events()
        self.start(students=False)
        self.assertEqual(self.item(event_id).status, 'blocked')
        while_disabled = self.event()
        self.start()
        self.assertEqual(self.service.queue_new_events(), 0)
        self.assertIsNone(self.item(while_disabled))

    def test_wrong_or_unsynced_branch_missing_number_photo_and_duplicates_do_not_qualify(self):
        for field, value in [('source_branch_id', None), ('source_branch_id', 'other'),
                             ('student_no', None), ('student_no', 'x' * 65), ('active', False)]:
            with self.subTest(field=field, value=value):
                if field == 'student_no' and value and len(value) > 64 and self.engine.dialect.name == 'postgresql':
                    with self.assertRaises(DataError), self.service.Session.begin() as session:
                        session.get(EduSchoolCatalogPerson, f'student:{STUDENT}').student_no = value
                    continue
                with self.service.Session.begin() as session:
                    person = session.get(EduSchoolCatalogPerson, f'student:{STUDENT}')
                    old = getattr(person, field)
                    setattr(person, field, value)
                self.start()
                with self.service.Session() as session:
                    self.assertFalse(session.get(EduSchoolCatalogPerson, f'student:{STUDENT}').attendance_approved)
                with self.service.Session.begin() as session:
                    setattr(session.get(EduSchoolCatalogPerson, f'student:{STUDENT}'), field, old)
        self.person('student', '661d00dea440164547761703')
        self.start()
        with self.service.Session() as session:
            self.assertFalse(session.get(EduSchoolCatalogPerson, f'student:{STUDENT}').attendance_approved)
        self.person('student', '661d00dea440164547761704', number='no-photo', photo=False)
        self.start()
        with self.service.Session() as session:
            self.assertFalse(session.get(EduSchoolCatalogPerson, 'student:661d00dea440164547761704').attendance_approved)

    def test_number_and_branch_changes_rechecked_before_delivery(self):
        for field, value in [('student_no', 'changed'), ('source_branch_id', 'another-branch')]:
            with self.subTest(field=field):
                self.start()
                event_id = self.event()
                self.service.queue_new_events()
                with self.service.Session.begin() as session:
                    person = session.get(EduSchoolCatalogPerson, f'student:{STUDENT}')
                    old = getattr(person, field)
                    setattr(person, field, value)
                with patch('core.eduschool.turnstile.send_attendance') as sender:
                    self.assertEqual(self.service.deliver_due(), 0)
                    sender.assert_not_called()
                self.assertEqual(self.item(event_id).status, 'blocked')
                with self.service.Session.begin() as session:
                    setattr(session.get(EduSchoolCatalogPerson, f'student:{STUDENT}'), field, old)

    def test_student_hold_and_mixed_identity_payload_cannot_send(self):
        self.start()
        held = self.event()
        self.service.queue_new_events()
        set_person_hold(f'student:{STUDENT}', True, self.engine)
        self.assertEqual(self.item(held).status, 'blocked')
        set_person_hold(f'student:{STUDENT}', False, self.engine)
        self.service.refresh_auto_approvals()
        malformed = self.event()
        self.service.queue_new_events()
        with self.service.Session.begin() as session:
            item = session.get(EduSchoolTurnstileOutbox, malformed)
            item.payload = {**item.payload, 'employeeNo': '15'}
        with patch('core.eduschool.turnstile.send_attendance') as sender:
            self.assertEqual(self.service.deliver_due(), 0)
            sender.assert_not_called()

    def test_old_staff_payload_gets_camera_flag(self):
        self.start()
        event_id = self.event('employee', STAFF)
        self.service.queue_new_events()
        with self.service.Session.begin() as session:
            item = session.get(EduSchoolTurnstileOutbox, event_id)
            item.payload = {k: v for k, v in item.payload.items() if k != 'isCamera'}
        with patch('core.eduschool.turnstile.send_attendance', return_value=DeliveryResult('sent')) as sender:
            self.assertEqual(self.service.deliver_due(), 1)
            self.assertIs(sender.call_args.args[1]['isCamera'], True)


class StudentMigrationTests(unittest.TestCase):
    def make_engine(self):
        return create_engine('sqlite://')

    def test_old_schema_upgrades_without_enabling_students_or_losing_staff(self):
        engine = self.make_engine()
        self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE eduschool_catalog_people (id VARCHAR(40) PRIMARY KEY, employee_no VARCHAR(64))'))
            conn.execute(text("INSERT INTO eduschool_catalog_people VALUES ('employee:test', '17')"))
            conn.execute(text('CREATE TABLE eduschool_turnstile_state (id INTEGER PRIMARY KEY, enabled BOOLEAN, activated_at TIMESTAMP, last_error TEXT)'))
            conn.execute(text('INSERT INTO eduschool_turnstile_state (id, enabled) VALUES (1, TRUE)'))
        self.assertIn('20261009_eduschool_student_attendance', run_migrations(engine))
        self.assertEqual(run_migrations(engine), [])
        self.assertIn('student_no', {c['name'] for c in inspect(engine).get_columns('eduschool_catalog_people')})
        with engine.connect() as conn:
            self.assertEqual(tuple(conn.execute(text('SELECT employee_no, student_no FROM eduschool_catalog_people')).one()), ('17', None))
            self.assertEqual(tuple(conn.execute(text('SELECT enabled, students_enabled, students_activated_at FROM eduschool_turnstile_state')).one()), (True, False, None))


@unittest.skipUnless(os.environ.get('STUDENT_TEST_DATABASE_URL'), 'isolated student PostgreSQL not supplied')
class PostgreSQLStudentTests(StudentAttendanceTests, StudentMigrationTests):
    def make_engine(self):
        url = os.environ['STUDENT_TEST_DATABASE_URL']
        admin = create_engine(url)
        schema = 'student_test_' + uuid.uuid4().hex
        with admin.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = create_engine(url, connect_args={'options': f'-csearch_path={schema} -ctimezone=Asia/Tashkent'})
        def cleanup():
            engine.dispose()
            with admin.begin() as conn:
                conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()
        self.addCleanup(cleanup)
        return engine


if __name__ == '__main__':
    unittest.main()
