from enum import Enum

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    Text,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

naming_convention = {
    "ix": 'ix_%(column_0_label)s',
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"
}
db = SQLAlchemy(metadata=MetaData(naming_convention=naming_convention))


class AbcCustomerEntities(Enum):
    AmoCustomer = 'amo_customers'
    AmoLead = 'amo_leads'
    AmoCompany = 'amo_companies'
    AmoContact = 'amo_contacts'
    SendMethods = 'send_methods'


class AbstractCustomer(db.Model):
    """
    "default" "general" : {
        "id": int
    }

    "amo_customers": [
        {
            "general": {
                "login": str,
                "billing_type": str,
                "is_robot": bool,
                "is_nds": bool,
                "system": str,
                "host": str,
                "name": str
            },
            "api_value": {}
        }
    ]

    "amo_leads": [
        {
            "general": {
                "login": Optional[str],
                "name": str
            }
            "api_value": {}
        }
    ]

    "amo_companies": [
        {
            "general": {
                "id": int,
                "inn": Optional[str],
                "name": str
            }
            "api_value": {}
        }
    ]

    "amo_contacts": [
        {
            "general": {
                "id": int,
                "phone": Optional[str],
                "name": str
            }
            "api_value": {}
        }
    ]

    "send_methods": [
        {
            "type": str (mail,sms,wtsp),
            "value": str,
            "desc": str,
        }
    ]
    """
    __tablename__ = "abstract_customer"
    id = Column(db.BigInteger(), nullable=False, primary_key=True)
    amo_customers = Column(JSONB)
    amo_leads = Column(JSONB)
    amo_companies = Column(JSONB)
    amo_contacts = Column(JSONB)
    send_methods = Column(JSONB)
    created_at = Column(db.DateTime, server_default=func.now())
    updated_at = Column(db.DateTime, onupdate=func.now())


class NewAbstractCustomer(db.Model):
    """
    Абстрактный покупатель, к которому привязаны сущности CRM.
    "send_methods": [
        {
            "type": str (mail,sms,wtsp),
            "value": str,
            "desc": str,
        }
    ]
    """
    __tablename__ = "abstract_customer"
    id = Column(BigInteger(), nullable=False, primary_key=True)
    send_methods = Column(JSONB)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())


class AmoCustomer(db.Model):
    """
    Покупатель из CRM, привязан к абстрактному покупателю.
    """
    __tablename__ = "amo_customer"
    amo_id = Column(BigInteger(), primary_key=True, autoincrement=False)
    abstract_id = Column(
        BigInteger(), ForeignKey("abstract_customer.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    login = Column(Text, index=True)
    name = Column(Text, index=True)
    billing_type = Column(Text)
    is_robot = Column(Boolean, nullable=False, server_default=false())
    is_nds = Column(Boolean, nullable=False, server_default=false())
    system = Column(Text)
    host = Column(Text)
    api_value = Column(JSONB, nullable=False)


class AmoLead(db.Model):
    """
    Сделка из CRM, привязана к абстрактному покупателю.
    """
    __tablename__ = "amo_lead"
    amo_id = Column(BigInteger(), primary_key=True, autoincrement=False)
    abstract_id = Column(
        BigInteger(), ForeignKey("abstract_customer.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    login = Column(Text, index=True)
    name = Column(Text, index=True)
    api_value = Column(JSONB, nullable=False)


class AbstractContact(db.Model):
    """
    Контакт CRM, привязанный к абстрактному покупателю.
    """
    __tablename__ = "abstract_contact"
    abstract_id = Column(
        BigInteger(), ForeignKey("abstract_customer.id", ondelete="CASCADE"),
        primary_key=True
    )
    amo_id = Column(BigInteger(), primary_key=True, autoincrement=False, index=True)
    api_value = Column(JSONB, nullable=False)


class AbstractCompany(db.Model):
    """
    Компания CRM, привязанная к абстрактному покупателю.
    """
    __tablename__ = "abstract_company"
    abstract_id = Column(
        BigInteger(), ForeignKey("abstract_customer.id", ondelete="CASCADE"),
        primary_key=True
    )
    amo_id = Column(BigInteger(), primary_key=True, autoincrement=False, index=True)
    api_value = Column(JSONB, nullable=False)
