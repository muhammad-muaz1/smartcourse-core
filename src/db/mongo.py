"""Client, index registration at startup, get_mongo dependency, check() for health.

serverSelectionTimeoutMS is set from settings rather than left at PyMongo's 30s default
— an unreachable Mongo must fail fast, not hang every operation (including the
buildInfo ping init_beanie runs) for half a minute. See docs/adr/0005.
"""

from typing import Any

from beanie import Document, init_beanie
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from src.config import MongoSettings

_client: AsyncMongoClient[dict[str, Any]] | None = None
_database: AsyncDatabase[dict[str, Any]] | None = None


async def init_mongo(
    settings: MongoSettings, document_models: list[type[Document]] | None = None
) -> None:
    global _client, _database
    _client = AsyncMongoClient(
        settings.url, serverSelectionTimeoutMS=settings.server_selection_timeout_ms
    )
    _database = _client.get_database(settings.database)
    ordered_models = sorted(document_models or [], key=lambda model: model.__name__)
    await init_beanie(database=_database, document_models=ordered_models)


async def close_mongo() -> None:
    if _client is not None:
        await _client.close()


def get_mongo() -> AsyncDatabase[dict[str, Any]]:
    if _database is None:
        raise RuntimeError("init_mongo() must run before get_mongo() is used")
    return _database


async def check() -> None:
    if _client is None:
        raise RuntimeError("init_mongo() must run before check() is used")
    await _client.get_database("admin").command("ping")
