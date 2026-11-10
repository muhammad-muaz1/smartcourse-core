"""This service's declarative Base — its own metadata, for its own database only."""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from platform_core.db import NAMING_CONVENTION, TimestampMixin, UUIDMixin

__all__ = ["Base", "TimestampMixin", "UUIDMixin"]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
