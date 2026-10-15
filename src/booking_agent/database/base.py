"""Declarative base shared by every ORM model."""

# MetaData holds table definitions and the constraint naming convention.
from sqlalchemy import MetaData

# DeclarativeBase is the SQLAlchemy 2.x typed base class.
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names make Alembic autogenerate diffs stable and readable.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for ORM models (no domain tables exist yet)."""

    # Shared metadata carrying the naming convention above.
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
