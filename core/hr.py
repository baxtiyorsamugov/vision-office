import time
from datetime import datetime
from database.models import Employee, Attendance
from database.manager import get_engine
from sqlalchemy.orm import sessionmaker

class HRManager:
    """Persist attendance only for device-local employee identities."""

    def __init__(self, cooldown_minutes=1, engine=None): # Для тестов ставим 1 минуту
        self.Session = sessionmaker(bind=engine or get_engine())
        self.cooldown = cooldown_minutes * 60
        self.last_seen = {} # Словарь памяти: Кто и когда был замечен последним
        self.employee_ids = self._load_employee_ids()

    def _load_employee_ids(self):
        session = self.Session()
        try:
            employees = session.query(Employee).all()
            return {emp.full_name: emp.id for emp in employees}
        finally:
            session.close()

    def register_presence(self, identity, event_type="entry"):
        """Store a local attendance event; ERP identities are deliberately skipped."""
        employee_id = None
        name = None
        subject_key = None
        if isinstance(identity, dict):
            person_id = str(identity.get("person_id") or "")
            if not person_id.startswith("local:") or identity.get("person_type") != "local_employee":
                return
            try:
                employee_id = int(person_id.removeprefix("local:"))
            except ValueError:
                return
            name = str(identity.get("name") or "")
            subject_key = person_id
        elif isinstance(identity, str):
            # Compatibility for older callers. New camera workers always pass
            # the structured local identity above.
            if identity in ["Неизвестный", "Анализ...", "Поиск лица..."]:
                return
            name = identity
            employee_id = self.employee_ids.get(name)
            subject_key = f"legacy:{name}"
        if not employee_id:
            return

        current_time = time.time()
        last_time = self.last_seen.get(subject_key, 0)

        # Если прошло больше времени, чем наш cooldown (таймаут)
        if current_time - last_time > self.cooldown:
            session = self.Session()
            
            # Verify the employee still exists before adding an attendance row.
            if session.get(Employee, employee_id) is not None:
                # Создаем запись о присутствии
                new_log = Attendance(
                    employee_id=employee_id, 
                    event_type=event_type,
                    timestamp=datetime.now()
                )
                session.add(new_log)
                session.commit()
                
                time_str = datetime.now().strftime("%H:%M:%S")
                action = "выход" if event_type == "exit" else "приход"
                print(f"📝 [HR LOG] Зафиксирован {action}: {name} в {time_str}")
                
            session.close()
            
            # Обновляем память системы
            self.last_seen[subject_key] = current_time
