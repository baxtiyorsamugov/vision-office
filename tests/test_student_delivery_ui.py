import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from streamlit.testing.v1 import AppTest
import streamlit as st

from core.edge.config import EdgeSettings
from core.eduschool.catalog import EduSchoolCatalogSettings
from database.models import (Base, EduSchoolCatalogPerson, EduSchoolTurnstileState,
                             EduSchoolTurnstileOutbox, EduSchoolDeliveryAttempt, RecognitionEvent)

STUDENT = '661d00dea440164547761701'


class StudentDeliveryUITests(unittest.TestCase):
    def test_student_profile_and_delivery_log_show_number_type_and_pause(self):
        st.cache_resource.clear()
        st.cache_data.clear()
        self.addCleanup(st.cache_resource.clear)
        self.addCleanup(st.cache_data.clear)
        engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(EduSchoolCatalogPerson(id=f'student:{STUDENT}', external_id=STUDENT, person_type='student',
                        full_name='Demo Student', student_no='S-TEST', source_status='active', active=True,
                        source_branch_id='test-branch', source_photo_status='missing'))
            session.add(EduSchoolTurnstileState(id=1, enabled=True, students_enabled=False))
            session.add(RecognitionEvent(id='test-event', person_id=f'edu:s:{STUDENT}', person_type='eduschool_student',
                        person_name='Demo Student', camera_id='test', event_type='entry', subject_signature='test'))
            session.add(EduSchoolTurnstileOutbox(event_id='test-event', person_id=f'student:{STUDENT}', status='sent'))
            session.add(EduSchoolDeliveryAttempt(id='test-attempt', event_id='test-event', attempt_number=1,
                        endpoint='https://backend.example.test/external-api/turnstile/attendance',
                        status='sent', http_status=200, api_code=0, finished_at=datetime.now(timezone.utc)))
            session.commit()
        with patch('database.manager.get_engine', return_value=engine), \
             patch('core.edge.config.load_edge_settings', return_value=EdgeSettings(enabled=False)), \
             patch('core.eduschool.catalog.load_settings', return_value=EduSchoolCatalogSettings(enabled=True)):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'ui/app.py'), default_timeout=30).run()
            app.session_state['eduschool_directory_type'] = 'Студенты'
            app.session_state['eduschool_profile_section_student'] = 'Отправка'
            app.radio[0].set_value('Сотрудники').run()
            self.assertFalse(app.exception)
            self.assertTrue(any('studentNo: S-TEST' in item.value for item in app.caption))
            self.assertTrue(any('платные SMS' in item.value for item in app.warning))
            self.assertTrue(any('Отправка учеников выключена' in item.value for item in app.info))
            hold = next(item for item in app.toggle if item.key == f'eduschool_hold_student:{STUDENT}')
            hold.set_value(True).run()
            self.assertFalse(app.exception)
            with Session(engine) as session:
                self.assertTrue(session.get(EduSchoolCatalogPerson, f'student:{STUDENT}').attendance_blocked)
            app.radio[0].set_value('Отправки').run()
            self.assertFalse(app.exception)
            next(item for item in app.selectbox if item.label == 'Категория').set_value('Ученики').run()
            self.assertFalse(app.exception)
            data = app.dataframe[0].value
            self.assertEqual(data.iloc[0]['Категория'], 'Ученик')
            self.assertEqual(data.iloc[0]['Номер'], 'S-TEST')
            next(item for item in app.text_input if item.label == 'Поиск').set_value('S-TEST').run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.dataframe[0].value), 1)
            app.session_state['delivery_log_view'] = 'Попытки HTTP'
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.dataframe[0].value.iloc[0]['HTTP'], 200)

    def test_legacy_sent_event_visible_without_fabricating_http_attempt(self):
        st.cache_resource.clear()
        st.cache_data.clear()
        self.addCleanup(st.cache_resource.clear)
        self.addCleanup(st.cache_data.clear)
        engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(EduSchoolTurnstileOutbox(event_id='legacy', person_id='employee:old',
                        status='sent', attempts=1, response_code=0, backend_event_id='backend-old'))
            session.add(EduSchoolTurnstileOutbox(event_id='skip', person_id='student:old',
                        status='skipped', attempts=0, last_error='Attendance number missing'))
            session.commit()
        with patch('database.manager.get_engine', return_value=engine), \
             patch('core.edge.config.load_edge_settings', return_value=EdgeSettings(enabled=False)), \
             patch('core.eduschool.catalog.load_settings', return_value=EduSchoolCatalogSettings(enabled=True)):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'ui/app.py'), default_timeout=30).run()
            app.radio[0].set_value('Отправки').run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.dataframe[0].value), 2)
            sent = app.dataframe[0].value.query("Событие == 'legacy'").iloc[0]
            self.assertEqual(sent['Результат'], 'Отправлено')
            self.assertEqual(sent['ID EduSchool'], 'backend-old')
            self.assertEqual(sent['Адрес последней попытки'], 'Не сохранён')
            self.assertTrue(any('без подробного HTTP-журнала' in item.value for item in app.warning))
            next(item for item in app.selectbox if item.label == 'Категория').set_value('Ученики').run()
            self.assertEqual(len(app.dataframe[0].value), 1)
            self.assertEqual(app.dataframe[0].value.iloc[0]['Результат'], 'Пропущено')
            next(item for item in app.text_input if item.label == 'Поиск').set_value('no-match').run()
            self.assertTrue(any('не найдено' in item.value for item in app.info))
            app.session_state['delivery_log_view'] = 'Попытки HTTP'
            app.run()
            self.assertFalse(app.exception)
            self.assertTrue(any('попыток отправки не найдено' in item.value for item in app.info))
            with Session(engine) as session:
                self.assertEqual(session.query(EduSchoolDeliveryAttempt).count(), 0)
                self.assertEqual(session.get(EduSchoolTurnstileOutbox, 'legacy').status, 'sent')


if __name__ == '__main__':
    unittest.main()
