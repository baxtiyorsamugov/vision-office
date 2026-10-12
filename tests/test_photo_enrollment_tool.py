import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from database.models import Base, EduSchoolCatalogPerson as Person, EduSchoolReferencePhoto as Photo
from tools.enroll_eduschool_photos import candidates


class EnrollmentSelectionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def person(self, key, **overrides):
        values = dict(id=key, external_id=key.zfill(24), person_type="student", full_name="Fixture",
                      source_status="active", active=True, source_photo_status="pending", image_url="fixture.jpg")
        values.update(overrides)
        self.session.add(Person(**values))

    def test_only_unenrolled_active_students_with_due_photos(self):
        self.person("pending")
        self.person("failed", source_photo_status="failed")
        self.person("staff", person_type="employee")
        self.person("inactive", active=False)
        self.person("missing", image_url=None)
        self.person("blank", image_url="")
        self.person("invalid", source_photo_status="invalid")
        self.person("ready", source_photo_status="ready")
        self.person("later", source_photo_status="failed", source_photo_retry_at=datetime.now(timezone.utc) + timedelta(hours=1))
        self.person("enrolled")
        self.session.add(Photo(id="photo", person_id="enrolled", photo_path="fixture.jpg",
                               image_checksum="fixture", embedding=[1.] * 512, active=True))
        self.session.commit()
        self.assertEqual({person.id for person in candidates(self.session, "student")}, {"pending", "failed"})


if __name__ == "__main__":
    unittest.main()
