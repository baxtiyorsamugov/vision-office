"""Operator-only ERP catalog transport; no changes to recognition or ERP sync."""
import logging
from datetime import datetime

import streamlit as st

from core.catalog_transfer import CatalogError, CatalogTransfer, MAX_ARCHIVE, digest

logger = logging.getLogger("vision_office.catalog_transfer")


def render_catalog_transfer(engine, settings, people):
    service = CatalogTransfer(engine, settings.base_url, reference_limit=settings.local_reference_photo_limit)
    with st.expander("Перенос каталога", icon=":material/folder_copy:"):
        export_tab, import_tab = st.tabs(["Экспорт", "Импорт"])
        with export_tab:
            labels = {person.id: f"{person.fio or 'Без имени'} · {person.id[:8]}" for person in people}
            all_people = st.checkbox(f"Все активные ERP-сотрудники ({len(labels)})", value=True, key="catalog_export_all")
            selected = list(labels) if all_people else st.multiselect(
                "Сотрудники для переноса", list(labels), format_func=labels.get, key="catalog_export_people")
            if st.button("Подготовить ZIP", icon=":material/archive:", disabled=not selected, key="catalog_export"):
                st.session_state.pop("catalog_export_result", None)
                try:
                    with st.spinner("Подготовка каталога…"):
                        raw, warnings = service.export(selected)
                    st.session_state["catalog_export_result"] = (raw, warnings, len(selected), datetime.now().strftime("%Y%m%d-%H%M%S"))
                except CatalogError as error:
                    st.error(str(error))
                except Exception as error:
                    logger.warning("Catalog export failed: %s", type(error).__name__)
                    st.error("Не удалось подготовить каталог. Проверьте доступ к базе, фото и моделям.")
            result = st.session_state.get("catalog_export_result")
            if result:
                raw, warnings, count, stamp = result
                st.success(f"Каталог готов: сотрудников {count}, размер {len(raw) / 1024 / 1024:.1f} МБ.")
                for warning in warnings[:10]:
                    st.warning(warning)
                if len(warnings) > 10:
                    st.warning(f"Всего предупреждений: {len(warnings)}.")
                st.download_button("Скачать каталог", raw, file_name=f"vision-office-erp-catalog-{stamp}.zip",
                                   mime="application/zip", icon=":material/download:", key="catalog_download")
                st.caption("Архив содержит персональные данные и биометрию. Он не зашифрован; храните его на защищённом носителе, не в GitHub.")
        with import_tab:
            receipt = st.session_state.get("catalog_import_receipt")
            if receipt:
                st.success(receipt)
            uploaded = st.file_uploader("Каталог сотрудников · ZIP до 100 МБ", type=["zip"], key="catalog_upload")
            if uploaded is None:
                st.session_state.pop("catalog_import_plan", None)
                return
            if uploaded.size > MAX_ARCHIVE:
                st.error("Размер архива превышает 100 МБ.")
                return
            raw = uploaded.getvalue()
            archive_id = digest(raw)
            recompute = st.checkbox("Пересчитать FaceID на CPU, если модели отличаются", key="catalog_recompute")
            if st.button("Проверить архив", icon=":material/fact_check:", key="catalog_validate"):
                st.session_state.pop("catalog_import_plan", None)
                st.session_state.pop("catalog_import_receipt", None)
                try:
                    with st.spinner("Проверка архива и фотографий…"):
                        prepared = service.prepare(service.read(raw), recompute=recompute)
                        preview = service.preview(prepared)
                    st.session_state["catalog_import_plan"] = (archive_id, prepared, preview)
                except CatalogError as error:
                    st.error(str(error))
                except Exception as error:
                    logger.warning("Catalog validation failed: %s", type(error).__name__)
                    st.error("Проверка не завершена. База не изменена; проверьте PostgreSQL и модели FaceID.")
            plan = st.session_state.get("catalog_import_plan")
            if not plan or plan[0] != archive_id:
                return
            _, prepared, preview = plan
            metrics = st.columns(3)
            metrics[0].metric("Новые сотрудники", preview["new"])
            metrics[1].metric("Дополнить записи", preview["fill"])
            metrics[2].metric("Фото в архиве", preview["photos"])
            st.dataframe(preview["rows"], hide_index=True, width="stretch")
            st.caption("Заполненные поля и статусы существующих сотрудников сохраняются. Посещения, настройки и ключи не переносятся.")
            st.caption("FaceID пересчитан из фото." if prepared.recomputed else "Модели FaceID совпадают; используются готовые векторы.")
            for warning in prepared.bundle.warnings[:10]:
                st.warning(warning)
            st.warning("Адрес ERP совпадает, но учебный центр автоматически не подтверждён. Импорт допустим только из доверенного каталога того же центра.")
            confirmed = st.checkbox("Подтверждаю: доверенный архив того же учебного центра", key=f"catalog_confirm_{archive_id}")
            if st.button("Импортировать в PostgreSQL", icon=":material/publish:", type="primary", disabled=not confirmed, key="catalog_apply"):
                try:
                    with st.spinner("Запись каталога…"):
                        result = service.apply(prepared, preview["state"], confirmed_same_center=confirmed)
                    st.session_state["catalog_import_receipt"] = (
                        f"Импорт завершён. Добавлено: {result['added']}; дополнено: {result['filled']}; дополнительных фото: {result['references']}."
                    )
                    st.session_state.pop("catalog_import_plan", None)
                    st.rerun()
                except CatalogError as error:
                    st.error(str(error))
                except Exception as error:
                    logger.warning("Catalog import failed: %s", type(error).__name__)
                    st.error("Импорт не подтверждён. Проверьте базу и выполните проверку архива заново перед повтором.")
