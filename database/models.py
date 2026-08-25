from sqlalchemy import Column, Integer, String, DateTime, Float, ForeignKey, JSON
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()

class Employee(Base):
    __tablename__ = 'employees'
    
    id = Column(Integer, primary_key=True)
    full_name = Column(String(100), nullable=False)
    face_embeddings = Column(JSON, nullable=False) 
    role = Column(String(50))
    # НОВАЯ КОЛОНКА ДЛЯ ФОТО:
    photo_path = Column(String(255), default="data/faces/default.png")

class Attendance(Base):
    __tablename__ = 'attendance'
    
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey('employees.id'))
    timestamp = Column(DateTime, default=datetime.now)
    event_type = Column(String(20))

class Violation(Base):
    __tablename__ = 'violations'
    
    id = Column(Integer, primary_key=True)
    camera_id = Column(String(50))
    violation_type = Column(String(50)) # "trash_detected"
    screenshot_path = Column(String(255))
    timestamp = Column(DateTime, default=datetime.now)