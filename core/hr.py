import time
from datetime import datetime
from database.models import Employee, Attendance
from database.manager import get_engine
from sqlalchemy.orm import sessionmaker

class HRManager:
    def __init__(self, cooldown_minutes=1): # Для тестов ставим 1 минуту
        self.Session = sessionmaker(bind=get_engine())
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

    def register_presence(self, name):
        if name in ["Неизвестный", "Анализ...", "Поиск лица..."]:
            
            return # Неизвестных в HR не пишем

        current_time = time.time()
        last_time = self.last_seen.get(name, 0)

        # Если прошло больше времени, чем наш cooldown (таймаут)
        if current_time - last_time > self.cooldown:
            session = self.Session()
            
            # Ищем сотрудника в БД, чтобы получить его ID
            employee_id = self.employee_ids.get(name)
            
            if employee_id:
                # Создаем запись о присутствии
                new_log = Attendance(
                    employee_id=employee_id, 
                    event_type="check_in", 
                    timestamp=datetime.now()
                )
                session.add(new_log)
                session.commit()
                
                time_str = datetime.now().strftime("%H:%M:%S")
                print(f"📝 [HR LOG] Зафиксирован приход: {name} в {time_str}")
                
            session.close()
            
            # Обновляем память системы
            self.last_seen[name] = current_time
