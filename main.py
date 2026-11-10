from amocrm import COMPANIES, CONTACTS, Customer, Lead, amocrm_get
from models import db
from sync import CrmSync


def collect_leads_customers(is_full=True):
    """
    Точка входа ночной синхронизации CRM -> Postgres.
    is_full=True  - полный прогон: в конце удаляются сущности, которых не было в выгрузке,
                    и записи с «удалить» в имени.
    is_full=False - частичный: пропускает закрытые статусы и ничего не удаляет.
    При любой ошибке транзакция откатывается.
    """
    customers = amocrm_get(Customer, _with=f"{COMPANIES},{CONTACTS}")
    leads = amocrm_get(Lead, _with=f"{CONTACTS}")
    CrmSync(db.session, customers, leads, is_full=is_full).run()
