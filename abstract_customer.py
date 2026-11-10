from typing import Optional

from amocrm import BaseEntity, Customer, Lead


def _fields(entity: BaseEntity) -> dict:
    return entity.custom_fields_values or {}


def get_login(entity: BaseEntity) -> Optional[str]:
    """Логин сущности из кастомного поля «Логин». None, если поля нет или оно пустое."""
    value = _fields(entity).get('Логин', {}).get('value')
    if value is None or value == "":
        return None
    return value if isinstance(value, str) else str(value)


def get_customer_row(customer: Customer) -> dict:
    """Значения колонок таблицы amo_customer (без abstract_id): выделенные поля + api_value."""
    fields = _fields(customer)
    return {
        'amo_id': customer.id,
        'login': get_login(customer),
        'name': customer.name,
        'billing_type': fields.get('Вид оплаты', {}).get('value'),
        'is_robot': bool(fields.get('Робот')),
        'is_nds': bool(fields.get('Оплата с НДС')),
        'system': fields.get('Система', {}).get('value'),
        'host': fields.get('Хост', {}).get('value'),
        'api_value': customer.as_dict(),
    }


def get_lead_row(lead: Lead) -> dict:
    """Значения колонок таблицы amo_lead (без abstract_id)."""
    return {
        'amo_id': lead.id,
        'login': get_login(lead),
        'name': lead.name,
        'api_value': lead.as_dict(),
    }
