from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from sqlalchemy import bindparam, delete, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from abstract_customer import get_customer_row, get_lead_row, get_login
from amocrm import Customer, Lead
from models import (
    AbstractCompany,
    AbstractContact,
    AbstractCustomer,
    AmoCustomer,
    AmoLead,
)

logger = logging.getLogger(__name__)

# Статусы CRM, пропускаемые в неполном прогоне
CUSTOMER_SKIP_STATUS_ID = 111349
LEAD_SKIP_STATUS_ID = 143

# Маркер для удаления сущности по имени
DELETE_MARKER = "удалить"

# Размер пакета строк в одном запросе на запись/удаление
BATCH_SIZE = 1000

# Переназначение абстрактного покупателя при слиянии.
_MOVE_ENTITIES_SQL = """
UPDATE {table} SET abstract_id = m.keep_id
FROM unnest(CAST(:drop_ids AS bigint[]), CAST(:keep_ids AS bigint[])) AS m(drop_id, keep_id)
WHERE {table}.abstract_id = m.drop_id
"""

# Переназначение вложенных сущностей при слиянии абстрактного покупателя.
# При совпадении при слиянии остается информация из итоговой записи
_COPY_LINKS_SQL = """
INSERT INTO {table} (abstract_id, amo_id, api_value)
SELECT DISTINCT ON (m.keep_id, t.amo_id) m.keep_id, t.amo_id, t.api_value
FROM {table} t
JOIN
    unnest(CAST(:drop_ids AS bigint[]), CAST(:keep_ids AS bigint[])) AS m(drop_id, keep_id)
    ON t.abstract_id = m.drop_id
ON CONFLICT (abstract_id, amo_id) DO NOTHING
"""


def chunks(items: Sequence[Any], size: int = BATCH_SIZE) -> Iterator[Sequence[Any]]:
    """Нарезает последовательность на чанки."""
    for start in range(0, len(items), size):
        yield items[start:start + size]


def merge_send_methods(*lists: Optional[List[dict]]) -> List[dict]:
    """
    Объединяет списки способов связи по составному ключу (type, value).
    Для дубликатов оставляет наиболее первое совпадение.
    """
    result: List[dict] = []
    seen: Set[Tuple[Any, Any]] = set()
    for methods in lists:
        for method in methods or []:
            key = (method.get("type"), method.get("value"))
            if key not in seen:
                seen.add(key)
                result.append(method)
    return result


class AbstractMerges:
    """Слияния абстрактных покупателей."""

    def __init__(self) -> None:
        self._merged_into: Dict[int, int] = {}

    def root(self, abstract_id: int) -> int:
        """Находит корневой id, в который будет слита запись."""
        while abstract_id in self._merged_into:
            abstract_id = self._merged_into[abstract_id]
        return abstract_id

    def merge(self, keep: int, drop: int) -> None:
        """Создает новую запись о слиянии."""
        self._merged_into[drop] = keep

    def as_mapping(self) -> Dict[int, int]:
        """Возвращает словарь вида: {поглощённый id: итоговый id}."""
        return {drop: self.root(drop) for drop in self._merged_into}


class EntityTable:
    """
    Состояние одного вида сущностей (покупатели или сделки) на время синхронизации.
    Хранит в памяти:
    * Мапы для связи идентификаторов из CRM с идентификаторами абстрактного клиента и с логином;
    * какие id были в БД до прогона, какие пришли из CRM и какие помечены словом «удалить»;
    * очередь строк, ожидающих записи в БД.
    """

    def __init__(self, model) -> None:
        self.model = model
        self.abstract_of: Dict[int, int] = {}
        self._login_of: Dict[int, str] = {}
        self._ids_by_login: Dict[str, Set[int]] = defaultdict(set)

        self.loaded: Set[int] = set()
        self.seen: Set[int] = set()
        self.to_delete: Set[int] = set()
        self.pending: Dict[int, dict] = {}

    def load(self, rows: Iterable[Tuple[int, int, Optional[str]]]) -> None:
        """Загружает сохранённые сущности: строки вида (id в CRM, abstract_id, логин)."""
        for entity_id, abstract_id, login in rows:
            self._index(entity_id, abstract_id, login)
        self.loaded = set(self.abstract_of)

    def add(self, row: dict, abstract_id: int) -> None:
        """Регистрирует сущность из CRM: обновляет индексы и ставит строку в очередь на запись."""
        entity_id = row["amo_id"]
        row["abstract_id"] = abstract_id
        self._index(entity_id, abstract_id, row["login"])
        self.seen.add(entity_id)
        if DELETE_MARKER in (row["name"] or "").lower():
            self.to_delete.add(entity_id)
        self.pending[entity_id] = row

    def abstract_ids_by_login(self, login: str) -> Set[int]:
        """Получает abstract_id по логину."""
        return {self.abstract_of[entity_id] for entity_id in self._ids_by_login.get(login, ())}

    def stale_ids(self) -> Set[int]:
        """Определяет множество id для удаления."""
        return (self.loaded - self.seen) | self.to_delete

    def _index(self, entity_id: int, abstract_id: int, login: Optional[str]) -> None:
        """Заполняет мапы сущности, учитывая возможное изменение логина в CRM"""
        old_login = self._login_of.pop(entity_id, None)
        if old_login:
            self._ids_by_login[old_login].discard(entity_id)
        self.abstract_of[entity_id] = abstract_id
        if login:
            self._login_of[entity_id] = login
            self._ids_by_login[login].add(entity_id)


class CrmSync:
    """Один прогон синхронизации.

    :param session: сессия SQLAlchemy.
    :param customers: поток покупателей из CRM.
    :param leads: поток сделок из CRM.
    :param is_full: полный прогон (с удалением устаревших данных) или частичный (без удаления).
    """

    def __init__(
        self, session, customers: Iterable[Customer], leads: Iterable[Lead],
        is_full: bool = True
    ) -> None:
        self.session = session
        self.is_full = is_full
        self._crm_customers = customers
        self._crm_leads = leads

        self.customers = EntityTable(AmoCustomer)
        self.leads = EntityTable(AmoLead)
        self.merges = AbstractMerges()

        self._contacts: Dict[Tuple[int, int], dict] = {}
        self._companies: Dict[Tuple[int, int], dict] = {}

        self._free_ids: deque = deque()
        self._new_abstract_ids: List[int] = []

    def run(self) -> None:
        """Запуск синхронизации. При любой ошибке транзакция полностью откатывается."""
        try:
            self._load_saved_state()
            if self.is_full:
                self._reset_links()

            logger.info(f"Collecting customers (is_full={self.is_full})")
            for customer in self._crm_customers:
                self._process_customer(customer)
                self._flush_if_batch_full()

            logger.info(f"Collecting leads (is_full={self.is_full})")
            for lead in self._crm_leads:
                self._process_lead(lead)
                self._flush_if_batch_full()

            # Выполняем добавление в БД
            self._flush()
            self._merge_abstracts()
            if self.is_full:
                self._delete_stale_entities()
                self._delete_empty_abstracts()
            self.session.commit()
        except Exception:
            # При ошибке осуществляется полный откат
            self.session.rollback()
            logger.exception("Синхронизация прервана, транзакция не применена, удаление не выполнялось.")
            raise
        logger.info(
            f"Синхронизация завершена: покупателей {len(self.customers.seen)}, "
            f"сделок {len(self.leads.seen)}, слияний {len(self.merges.as_mapping())}"
        )

    def _load_saved_state(self) -> None:
        """Загружает в память идентифицирующие колонки сохранённых покупателей и сделок."""
        for table in (self.customers, self.leads):
            model = table.model
            table.load(self.session.execute(select(model.amo_id, model.abstract_id, model.login)))

    def _reset_links(self) -> None:
        """
        Удаляет все контакты и компании, чтобы пересоздать их из выгрузки.
        В случае ошибки удаленное возвращается при откате.
        """
        for model in (AbstractContact, AbstractCompany):
            self.session.execute(delete(model.__table__))

    def _process_customer(self, customer: Customer) -> None:
        """Определяет абстрактного покупателя для покупателя CRM."""
        if not self.is_full and customer.status_id == CUSTOMER_SKIP_STATUS_ID:
            return
        login = get_login(customer)
        if not login:
            return

        # Поиск покупателя по id покупателя в CRM
        if customer.id in self.customers.abstract_of:
            abstract_id = self.merges.root(self.customers.abstract_of[customer.id])
        # Поиск покупателя по логину среди сохраненных сделок
        else:
            abstract_id = self._abstract_by_login(login, self.leads)
            if abstract_id is None:
                abstract_id = self._new_abstract_id()

        self.customers.add(get_customer_row(customer), abstract_id)
        self._queue_links(abstract_id, customer)

    def _process_lead(self, lead: Lead) -> None:
        """Определяет абстрактного покупателя для сделки CRM и при необходимости сливает записи."""
        if not self.is_full and lead.status_id == LEAD_SKIP_STATUS_ID:
            return
        login = get_login(lead)

        by_id = None
        if lead.id in self.leads.abstract_of:
            by_id = self.merges.root(self.leads.abstract_of[lead.id])
        by_login = self._abstract_by_login(login, self.customers) if login else None

        if by_id is None:
            abstract_id = by_login
        else:
            abstract_id = by_id
            # Слияние двух разных записей по id в CRM и по логину покупателя
            if by_login is not None and by_login != by_id:
                self.merges.merge(keep=by_id, drop=by_login)
        if abstract_id is None:
            abstract_id = self._new_abstract_id()

        self.leads.add(get_lead_row(lead), abstract_id)
        self._queue_links(abstract_id, lead)

    def _abstract_by_login(self, login: str, table: EntityTable) -> Optional[int]:
        """
        Поиск абстрактного покупателя по логину в указанной сущности.
        Совпадение используется, только если оно однозначно
        """
        roots = {self.merges.root(abstract_id) for abstract_id in table.abstract_ids_by_login(login)}
        if len(roots) == 1:
            return roots.pop()
        if roots:
            logger.warning(f"Login {login} has more than one abccustomer")
        return None

    def _new_abstract_id(self) -> int:
        """Выдаёт id для новой записи abstract_customer. Строка в БД создаётся при ближайшей записи пакета."""
        if not self._free_ids:
            ids = self.session.execute(
                text("SELECT nextval(pg_get_serial_sequence('abstract_customer', 'id')) "
                     "FROM generate_series(1, :n)"),
                {"n": BATCH_SIZE},
            ).scalars().all()
            self._free_ids.extend(ids)
        abstract_id = self._free_ids.popleft()
        self._new_abstract_ids.append(abstract_id)
        return abstract_id

    def _queue_links(self, abstract_id: int, entity: Any) -> None:
        """Связывает абстрактного покупателя с контактами и компаниями сущности из CRM."""
        for contact in entity.contacts:
            self._contacts[(abstract_id, contact["id"])] = contact
        for company in entity.companies:
            self._companies[(abstract_id, company["id"])] = company

    def _flush_if_batch_full(self) -> None:
        """Проверяет текущий буфер на переполнение лимита."""
        if len(self.customers.pending) + len(self.leads.pending) >= BATCH_SIZE:
            self._flush()

    def _flush(self) -> None:
        """Записывает накопленное в БД. Порядок важен из-за внешних ключей: сначала абстрактные покупатели."""
        table = AbstractCustomer.__table__
        for chunk in chunks(self._new_abstract_ids):
            self.session.execute(table.insert(), [{"id": abstract_id} for abstract_id in chunk])
        self._new_abstract_ids.clear()

        self._upsert_entities(self.customers)
        self._upsert_entities(self.leads)
        self._upsert_links(AbstractContact, self._contacts)
        self._upsert_links(AbstractCompany, self._companies)

    def _upsert_entities(self, entities: EntityTable) -> None:
        """Вставляет или обновляет строки покупателей/сделок."""
        if not entities.pending:
            return

        table = entities.model.__table__
        columns = [column.name for column in table.columns if column.name != "amo_id"]
        insert = pg_insert(table)
        statement = insert.on_conflict_do_update(
            index_elements=[table.c.amo_id],
            set_={name: insert.excluded[name] for name in columns},
            where=or_(*[table.c[name].is_distinct_from(insert.excluded[name]) for name in columns]),
        )

        rows = list(entities.pending.values())
        for chunk in chunks(rows):
            self.session.execute(statement, chunk)
        entities.pending.clear()

    def _upsert_links(self, model, pending: Dict[Tuple[int, int], dict]) -> None:
        """Вставляет или обновляет контакты/компании абстрактных покупателей."""
        if not pending:
            return

        table = model.__table__
        insert = pg_insert(table)
        statement = insert.on_conflict_do_update(
            index_elements=[table.c.abstract_id, table.c.amo_id],
            set_={"api_value": insert.excluded.api_value},
        )

        rows = [
            {"abstract_id": abstract_id, "amo_id": amo_id, "api_value": api_value}
            for (abstract_id, amo_id), api_value in pending.items()
        ]
        for chunk in chunks(rows):
            self.session.execute(statement, chunk)
        pending.clear()

    def _merge_abstracts(self) -> None:
        """
        Слияние данных абстрактных покупателей.
        Сначала выполняется слияние, затем удаление слитой записи для предотвращения каскадного удаления
        связанной информации.
        """
        merged = self.merges.as_mapping()
        if not merged:
            return
        logger.info(f"Слияние абстрактных покупателей: {len(merged)}")

        self._merge_send_methods(merged)

        for chunk in chunks(list(merged)):
            params = {"drop_ids": list(chunk), "keep_ids": [merged[drop] for drop in chunk]}
            for model in (AmoCustomer, AmoLead):
                self.session.execute(text(_MOVE_ENTITIES_SQL.format(table=model.__tablename__)), params)
            for model in (AbstractContact, AbstractCompany):
                self.session.execute(text(_COPY_LINKS_SQL.format(table=model.__tablename__)), params)

        table = AbstractCustomer.__table__
        for chunk in chunks(list(merged)):
            self.session.execute(delete(table).where(table.c.id.in_(chunk)))

    def _merge_send_methods(self, merged: Dict[int, int]) -> None:
        """Объединяет способы связи поглощённых записей астрактных покупателей в итоговую."""
        drops_by_keep: Dict[int, List[int]] = defaultdict(list)
        for drop, keep in merged.items():
            drops_by_keep[keep].append(drop)

        table = AbstractCustomer.__table__
        involved = list(set(merged) | set(drops_by_keep))
        current: Dict[int, Optional[List[dict]]] = {}
        for chunk in chunks(involved):
            rows = self.session.execute(select(table.c.id, table.c.send_methods).where(table.c.id.in_(chunk)))
            current.update({row[0]: row[1] for row in rows})

        updates = []
        for keep, drops in drops_by_keep.items():
            combined = merge_send_methods(current.get(keep), *[current.get(drop) for drop in drops])
            if combined and combined != current.get(keep):
                updates.append({"keep_id": keep, "methods": combined})

        if updates:
            statement = (
                update(table).where(table.c.id == bindparam("keep_id")).values(
                    send_methods=bindparam("methods", type_=JSONB)
                )
            )
            for chunk in chunks(updates):
                self.session.execute(statement, chunk)

    def _delete_stale_entities(self) -> None:
        """Удаляет покупателей и сделки, которых не было в выгрузке или с пометкой удаления в имени."""
        for entities in (self.customers, self.leads):
            table = entities.model.__table__
            stale = list(entities.stale_ids())
            for chunk in chunks(stale):
                self.session.execute(delete(table).where(table.c.amo_id.in_(chunk)))
            logger.info(f"Чистка {table.name}: удалено {len(stale)}")

    def _delete_empty_abstracts(self) -> None:
        """Удаляет абстрактных покупателей, у которых не осталось связанных покупателей или сделок."""
        self.session.execute(
            text("""DELETE FROM abstract_customer a
            WHERE NOT EXISTS (SELECT 1 FROM amo_customer c WHERE c.abstract_id = a.id)
              AND NOT EXISTS (SELECT 1 FROM amo_lead l WHERE l.abstract_id = a.id)""")
        )
