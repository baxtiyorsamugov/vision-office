"""Read-only delivery history, including records predating HTTP attempt logging."""

from sqlalchemy import and_, func

from database.models import (EduSchoolCatalogPerson, EduSchoolDeliveryAttempt,
                             EduSchoolTurnstileOutbox, RecognitionEvent)


def delivery_history_query(session):
    latest = session.query(
        EduSchoolDeliveryAttempt.event_id.label("event_id"),
        func.max(EduSchoolDeliveryAttempt.attempt_number).label("attempt_number"),
    ).group_by(EduSchoolDeliveryAttempt.event_id).subquery()
    return session.query(
        EduSchoolTurnstileOutbox, RecognitionEvent, EduSchoolCatalogPerson, EduSchoolDeliveryAttempt,
    ).select_from(EduSchoolTurnstileOutbox).outerjoin(
        RecognitionEvent, RecognitionEvent.id == EduSchoolTurnstileOutbox.event_id,
    ).outerjoin(
        EduSchoolCatalogPerson, EduSchoolCatalogPerson.id == EduSchoolTurnstileOutbox.person_id,
    ).outerjoin(latest, latest.c.event_id == EduSchoolTurnstileOutbox.event_id).outerjoin(
        EduSchoolDeliveryAttempt, and_(
            EduSchoolDeliveryAttempt.event_id == latest.c.event_id,
            EduSchoolDeliveryAttempt.attempt_number == latest.c.attempt_number,
        ),
    )
