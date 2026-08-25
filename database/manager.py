from sqlalchemy import create_engine
from database.models import Base
import yaml
import os

def get_engine():
    with open("config/settings.yaml", "r") as f:
        config = yaml.safe_load(f)
    
    db_path = config['database']['path']
    # Создаем папку data, если её нет
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return create_engine(f'sqlite:///{db_path}')

def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    print("✅ База данных успешно развернута.")

if __name__ == "__main__":
    init_db()