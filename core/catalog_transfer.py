"""Local ERP catalog bundles. No ERP calls, credentials, events or migrations."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageOps
from PIL.JpegImagePlugin import JpegImageFile
from PIL.PngImagePlugin import PngImageFile
from sqlalchemy.orm import Session

from database.models import RemotePerson, RemotePersonReferencePhoto

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "vision-office.erp-catalog"
MAX_ARCHIVE = 100 * 1024 * 1024
MAX_UNPACKED = 150 * 1024 * 1024
MAX_PHOTO = 10 * 1024 * 1024
MAX_MANIFEST = 16 * 1024 * 1024
MAX_PEOPLE = 1000
MODEL_FILES = ("det_10g.onnx", "w600k_r50.onnx")


class CatalogError(ValueError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("Повторяющееся поле в манифесте каталога.")
        result[key] = value
    return result


def origin(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise CatalogError("Укажите адрес ERP без логина и пароля в URL.")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@lru_cache(maxsize=16)
def _model_hash(path, size, mtime):
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def model_identity(root=ROOT):
    result = {}
    for name in MODEL_FILES:
        path = Path(root) / "models/insightface/models/buffalo_l" / name
        if not path.is_file():
            raise CatalogError(f"Отсутствует модель FaceID: {name}. Сначала установите модели.")
        stat = path.stat()
        result[name] = _model_hash(str(path), stat.st_size, stat.st_mtime_ns)
    return result


def vector(value):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 512 or any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
        raise CatalogError("Некорректный вектор FaceID: нужны 512 конечных чисел.")
    norm = math.sqrt(sum(v * v for v in value))
    if not math.isfinite(norm) or norm < 1e-12:
        raise CatalogError("Некорректная норма вектора FaceID.")
    return value


def valid_vector(value):
    try:
        return vector(value) is not None
    except CatalogError:
        return False


def photo_path(root, value):
    if not value:
        return None
    path = (Path(root) / value).resolve()
    allowed = (Path(root) / "data/persons").resolve()
    if not allowed.is_relative_to(Path(root).resolve()) or not path.is_relative_to(allowed):
        raise CatalogError("Фотография находится вне каталога data/persons.")
    return path if path.is_file() else None


def open_photo(raw):
    # Use bounded JPEG/PNG decoders directly: YOLO patches Image.open globally
    # and can attempt to install HEIF plugins when an unrelated file is corrupt.
    if raw.startswith(b"\xff\xd8\xff"):
        return JpegImageFile(io.BytesIO(raw))
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return PngImageFile(io.BytesIO(raw))
    raise CatalogError("Допустимы только фотографии JPEG/PNG.")


def check_image(raw):
    if not raw or len(raw) > MAX_PHOTO:
        raise CatalogError("Фотография пуста или больше 10 МБ.")
    try:
        with open_photo(raw) as image:
            if image.format not in {"JPEG", "PNG"} or image.width * image.height > 20_000_000:
                raise CatalogError("Допустимы JPEG/PNG до 20 мегапикселей.")
            image.verify()
        with open_photo(raw) as image:
            image.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
        raise CatalogError("Повреждённая фотография в каталоге.") from error


def _text(value, limit, label, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise CatalogError(f"Некорректное поле: {label}.")
    return value


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CatalogError("Некорректная контрольная сумма.")
    return value


def _photo_url(value):
    # Signed photo URLs can contain bearer credentials; never put them in bundles.
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.hostname and not (parsed.username or parsed.password or parsed.query or parsed.fragment):
        return value
    return None


@dataclass
class Bundle:
    manifest: dict
    photos: dict[str, bytes]
    sha256: str
    warnings: list[str]


@dataclass
class PreparedImport:
    bundle: Bundle
    model: dict
    people: list[dict]
    recomputed: bool


class CatalogTransfer:
    def __init__(self, engine, erp_url, root=ROOT, reference_limit=10):
        self.engine = engine
        self.erp_origin = origin(erp_url)
        self.root = Path(root).resolve()
        self.reference_limit = reference_limit

    def export(self, person_ids):
        ids = sorted({str(uuid.UUID(value)) for value in person_ids})
        if not ids or len(ids) > MAX_PEOPLE:
            raise CatalogError("Выберите от 1 до 1000 сотрудников.")
        photos, warnings, people = {}, [], []

        def pack_photo(path_value, label):
            path = photo_path(self.root, path_value)
            if not path:
                warnings.append(f"{label}: фотография отсутствует.")
                return None
            if path.stat().st_size > MAX_PHOTO:
                raise CatalogError("Фотография больше 10 МБ.")
            raw = path.read_bytes()
            check_image(raw)
            # Strip EXIF/location and normalise format without exposing source paths.
            with open_photo(raw) as image:
                buffer = io.BytesIO()
                ImageOps.exif_transpose(image).convert("RGB").save(buffer, format="JPEG", quality=95)
                raw = buffer.getvalue()
            name = f"photos/{digest(raw)}.jpg"
            photos[name] = raw
            if sum(map(len, photos.values())) > MAX_ARCHIVE:
                raise CatalogError("Каталог слишком большой: выберите меньше сотрудников.")
            return name

        with Session(self.engine) as session:
            # One snapshot for people and their extra photos; reads never lock cameras.
            if self.engine.dialect.name == "postgresql":
                session.connection().exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            rows = session.query(RemotePerson).filter(RemotePerson.id.in_(ids)).order_by(RemotePerson.id).all()
            if len(rows) != len(ids):
                raise CatalogError("Каталог изменился: обновите список сотрудников.")
            extras = session.query(RemotePersonReferencePhoto).filter(RemotePersonReferencePhoto.person_id.in_(ids)).order_by(RemotePersonReferencePhoto.id).all()
            for row in rows:
                person = {"id": row.id, "fio": row.fio, "person_type": row.person_type, "active": bool(row.active),
                          "updated_at": utc(row.updated_at).isoformat(), "photo_url": _photo_url(row.photo_url),
                          "photo": pack_photo(row.photo_path, row.fio or row.id),
                          "embedding": row.embedding if valid_vector(row.embedding) else None, "references": []}
                for extra in extras:
                    if extra.person_id != row.id:
                        continue
                    photo = pack_photo(extra.photo_path, f"{row.fio or row.id}, дополнительное фото") if valid_vector(extra.embedding) else None
                    if photo and valid_vector(extra.embedding):
                        person["references"].append({"photo": photo, "image_checksum": extra.image_checksum,
                                                     "embedding": extra.embedding, "active": bool(extra.active),
                                                     "created_at": utc(extra.created_at).isoformat()})
                    else:
                        warnings.append(f"{row.fio or row.id}: дополнительный шаблон пропущен.")
                people.append(person)
        manifest = {"schema": SCHEMA, "version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                    "erp_origin": self.erp_origin, "model": model_identity(self.root), "people": people}
        raw_manifest = json_bytes(manifest)
        if len(raw_manifest) > MAX_MANIFEST:
            raise CatalogError("Манифест слишком большой: выберите меньше сотрудников.")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("catalog.json", raw_manifest)
            for name, raw in photos.items():
                archive.writestr(name, raw)
        raw = output.getvalue()
        if len(raw) > MAX_ARCHIVE:
            raise CatalogError("Архив больше 100 МБ.")
        # Run the same strict reader before offering an export to the operator.
        warnings.extend(self.read(raw).warnings)
        return raw, list(dict.fromkeys(warnings))

    def read(self, raw):
        if len(raw) > MAX_ARCHIVE:
            raise CatalogError("Архив больше 100 МБ.")
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                names = [item.filename for item in entries]
                if len(entries) > 5001 or len(set(names)) != len(names) or sum(item.file_size for item in entries) > MAX_UNPACKED:
                    raise CatalogError("Некорректный или слишком большой ZIP.")
                for item in entries:
                    if item.flag_bits & 1 or (item.external_attr >> 16) & 0o170000 == 0o120000:
                        raise CatalogError("Зашифрованные ZIP и ссылки не поддерживаются.")
                    if item.filename != "catalog.json" and not re.fullmatch(r"photos/[0-9a-f]{64}\.jpg", item.filename):
                        raise CatalogError("В ZIP обнаружен посторонний или небезопасный путь.")
                    if item.file_size > (MAX_MANIFEST if item.filename == "catalog.json" else MAX_PHOTO):
                        raise CatalogError("Файл в ZIP превышает допустимый размер.")
                manifest = json.loads(archive.read("catalog.json"), object_pairs_hook=unique_object)
                if not isinstance(manifest, dict) or set(manifest) != {"schema", "version", "created_at", "erp_origin", "model", "people"}:
                    raise CatalogError("Неизвестный формат каталога.")
                if manifest["schema"] != SCHEMA or type(manifest["version"]) is not int or manifest["version"] != 1:
                    raise CatalogError("Версия каталога не поддерживается.")
                utc(manifest["created_at"])
                if manifest["erp_origin"] != self.erp_origin:
                    raise CatalogError("Архив относится к другому адресу ERP.")
                if set(manifest["model"]) != set(MODEL_FILES):
                    raise CatalogError("Нет идентификатора моделей FaceID.")
                for value in manifest["model"].values():
                    _sha(value)
                people = manifest["people"]
                if not isinstance(people, list) or not 1 <= len(people) <= MAX_PEOPLE:
                    raise CatalogError("Некорректное количество сотрудников.")
                seen, referenced = set(), set()
                for person in people:
                    if set(person) != {"id", "fio", "person_type", "active", "updated_at", "photo_url", "photo", "embedding", "references"}:
                        raise CatalogError("Неизвестные поля сотрудника.")
                    pid = str(uuid.UUID(person["id"]))
                    if pid != person["id"] or pid in seen:
                        raise CatalogError("Повторяющийся или некорректный ERP ID.")
                    seen.add(pid)
                    _text(person["fio"], 255, "ФИО", nullable=True)
                    if person["person_type"] not in {"employee", "teacher", "student"} or type(person["active"]) is not bool:
                        raise CatalogError("Некорректный тип или статус сотрудника.")
                    utc(person["updated_at"])
                    if person["photo_url"] is not None and (_photo_url(person["photo_url"]) != person["photo_url"] or len(person["photo_url"]) > 1024):
                        raise CatalogError("Небезопасный URL фотографии.")
                    vector(person["embedding"])
                    if person["photo"] is not None:
                        referenced.add(person["photo"])
                    refs = person["references"]
                    if not isinstance(refs, list) or len(refs) > 30:
                        raise CatalogError("Слишком много дополнительных фото.")
                    checksums = set()
                    for extra in refs:
                        if set(extra) != {"photo", "image_checksum", "embedding", "active", "created_at"}:
                            raise CatalogError("Неизвестные поля дополнительного фото.")
                        checksum = _sha(extra["image_checksum"])
                        if checksum in checksums or type(extra["active"]) is not bool or vector(extra["embedding"]) is None:
                            raise CatalogError("Некорректное дополнительное фото.")
                        checksums.add(checksum)
                        utc(extra["created_at"])
                        referenced.add(extra["photo"])
                if not referenced.issubset(set(names)):
                    raise CatalogError("В архиве не хватает фотографий.")
                photos = {}
                for name in names:
                    if name == "catalog.json":
                        continue
                    value = archive.read(name)
                    if digest(value) != Path(name).stem:
                        raise CatalogError("Контрольная сумма фотографии не совпадает.")
                    check_image(value)
                    photos[name] = value
                if referenced != set(photos):
                    raise CatalogError("Архив содержит фотографии без владельца.")
                warnings = [f"{p['fio'] or p['id']}: фотография отсутствует." for p in people if p["photo"] is None]
                warnings.extend(f"{p['fio'] or p['id']}: нет готового основного FaceID." for p in people if p["embedding"] is None)
                return Bundle(manifest, photos, digest(raw), warnings)
        except CatalogError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
            raise CatalogError("Архив повреждён или не соответствует формату каталога.") from error

    def prepare(self, bundle, recompute=False, embedding_fn=None):
        model = model_identity(self.root)
        people = json.loads(json_bytes(bundle.manifest["people"]))
        mismatch = model != bundle.manifest["model"]
        if mismatch and not recompute:
            raise CatalogError("Модели FaceID отличаются. Требуется пересчёт векторов из фотографий.")
        if mismatch:
            if embedding_fn is None:
                from core.ai.recognizer import FaceRecognizer
                import cv2
                import numpy as np
                recognizer = FaceRecognizer(use_cuda=False)
                embedding_fn = lambda raw: recognizer.get_embedding(cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR))
            computed = {}
            for person in people:
                for item in [person, *person["references"]]:
                    name = item["photo"]
                    if not name:
                        raise CatalogError("Для пересчёта нужны основные фотографии всех сотрудников.")
                    if name not in computed:
                        result = embedding_fn(bundle.photos[name])
                        result = result.tolist() if hasattr(result, "tolist") else result
                        if vector(result) is None:
                            raise CatalogError("На одном из фото не найдено лицо. Импорт не выполнен.")
                        computed[name] = result
                        time.sleep(0.1)
                    item["embedding"] = computed[name]
        return PreparedImport(bundle, model, people, mismatch)

    def _plan(self, session, prepared):
        ids = [p["id"] for p in prepared.people]
        existing = {p.id: p for p in session.query(RemotePerson).filter(RemotePerson.id.in_(ids)).all()}
        extras = session.query(RemotePersonReferencePhoto).filter(RemotePersonReferencePhoto.person_id.in_(ids)).order_by(RemotePersonReferencePhoto.id).all()
        state, actions = [], []
        for person in prepared.people:
            row = existing.get(person["id"])
            fields = []
            if row:
                has_photo = photo_path(self.root, row.photo_path) is not None
                state.append({"person": {c.name: (utc(getattr(row, c.name)).isoformat() if c.name == "updated_at" else getattr(row, c.name)) for c in RemotePerson.__table__.columns}, "photo_exists": has_photo})
                if not row.fio and person["fio"]:
                    fields.append("fio")
                # A changed official photo URL means the old bundle may be stale.
                same_photo = not row.photo_url or row.photo_url == person["photo_url"]
                if same_photo:
                    if not has_photo and person["photo"]:
                        fields.append("photo_path")
                    if not valid_vector(row.embedding) and person["embedding"]:
                        fields.append("embedding")
                    if not row.photo_url and person["photo_url"]:
                        fields.append("photo_url")
            references = [p for p in extras if p.person_id == person["id"]]
            known = {p.image_checksum for p in references}
            active_count = sum(p.active for p in references)
            add_refs = []
            skipped = 0
            for extra in person["references"]:
                if extra["image_checksum"] in known:
                    continue
                if extra["active"] and active_count >= self.reference_limit:
                    skipped += 1
                    continue
                add_refs.append(extra)
                active_count += extra["active"]
            actions.append({"person": person, "new": row is None, "fields": fields, "references": add_refs, "skipped": skipped})
        state.extend({c.name: (utc(getattr(row, c.name)).isoformat() if c.name == "created_at" else getattr(row, c.name)) for c in RemotePersonReferencePhoto.__table__.columns} for row in extras)
        return actions, digest(json_bytes(state))

    def preview(self, prepared):
        with Session(self.engine) as session:
            actions, state = self._plan(session, prepared)
        rows = [{"ERP ID": a["person"]["id"], "ФИО": a["person"]["fio"] or "Без имени",
                 "Действие": "Добавить" if a["new"] else ("Дополнить" if a["fields"] or a["references"] else "Сохранить без изменений"),
                 "Поля": ", ".join(a["fields"]), "Доп. фото": len(a["references"]), "Пропущено по лимиту": a["skipped"]} for a in actions]
        return {"state": state, "rows": rows, "new": sum(a["new"] for a in actions),
                "fill": sum(not a["new"] and bool(a["fields"] or a["references"]) for a in actions),
                "references": sum(len(a["references"]) for a in actions), "photos": len(prepared.bundle.photos)}

    def apply(self, prepared, expected_state, confirmed_same_center=False):
        if not confirmed_same_center:
            raise CatalogError("Подтвердите, что это тот же учебный центр.")
        if model_identity(self.root) != prepared.model:
            raise CatalogError("Модели изменились после проверки. Проверьте архив заново.")
        folder = self.root / "data/persons/catalog" / uuid.uuid4().hex
        if not folder.resolve().is_relative_to(self.root):
            raise CatalogError("Папка фотографий выходит за пределы проекта.")
        written, commit_started = [], False

        def save(name):
            if name is None:
                return None
            target = folder / Path(name).name
            if target not in written:
                folder.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    written.append(target)
                    stream.write(prepared.bundle.photos[name])
                    stream.flush()
                    os.fsync(stream.fileno())
            return target.relative_to(self.root).as_posix()

        try:
            with self.engine.begin() as connection:
                if self.engine.dialect.name == "postgresql":
                    connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                    connection.exec_driver_sql("LOCK TABLE remote_persons, remote_person_reference_photos IN SHARE ROW EXCLUSIVE MODE")
                elif self.engine.dialect.name == "sqlite":
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                with Session(bind=connection) as session:
                    actions, current_state = self._plan(session, prepared)
                    if current_state != expected_state:
                        raise CatalogError("Каталог изменился после проверки. Нажмите «Проверить архив» ещё раз.")
                    for action in actions:
                        person = action["person"]
                        row = session.get(RemotePerson, person["id"])
                        if action["new"]:
                            row = RemotePerson(id=person["id"], fio=person["fio"], person_type=person["person_type"],
                                               active=person["active"], updated_at=utc(person["updated_at"]),
                                               photo_url=person["photo_url"], photo_path=save(person["photo"]),
                                               embedding=person["embedding"], embedding_status="ready" if person["embedding"] else "pending")
                            session.add(row)
                        else:
                            for field in action["fields"]:
                                if field == "photo_path":
                                    row.photo_path = save(person["photo"])
                                else:
                                    setattr(row, field, person[field])
                                if field == "embedding":
                                    row.embedding_status, row.embedding_error = "ready", None
                        session.flush()
                        for extra in action["references"]:
                            session.add(RemotePersonReferencePhoto(id=str(uuid.uuid4()), person_id=person["id"], source="local",
                                                                  image_checksum=extra["image_checksum"], embedding=extra["embedding"],
                                                                  active=extra["active"], created_at=utc(extra["created_at"]), photo_path=save(extra["photo"])))
                    session.flush()
                    commit_started = True
            return {"added": sum(a["new"] for a in actions), "filled": sum(not a["new"] and bool(a["fields"]) for a in actions),
                    "references": sum(len(a["references"]) for a in actions)}
        except Exception:
            # A lost connection during COMMIT has an uncertain outcome. Retain
            # files in that case so a committed row can never point to deleted media.
            if not commit_started:
                for path in written:
                    path.unlink(missing_ok=True)
                if folder.is_dir():
                    folder.rmdir()
            raise
