"""Requires Docker (testcontainers spins up a real Mongo)."""

from collections.abc import AsyncIterator

import pytest
from beanie import Document
from testcontainers.community.mongodb import MongoDbContainer

from src.config import MongoSettings
from src.db import mongo


class _Note(Document):
    text: str

    class Settings:
        name = "test_notes"


@pytest.fixture
async def _mongo(mongo_container: MongoDbContainer) -> AsyncIterator[None]:
    settings = MongoSettings(url=mongo_container.get_connection_url(), database="smartcourse_test")
    await mongo.init_mongo(settings, document_models=[_Note])
    try:
        yield
    finally:
        database = mongo.get_mongo()
        await database.client.drop_database("smartcourse_test")
        await mongo.close_mongo()


async def test_check_succeeds_against_a_real_database(_mongo: None) -> None:
    await mongo.check()  # must not raise


async def test_get_mongo_returns_the_initialised_database(_mongo: None) -> None:
    database = mongo.get_mongo()
    assert database.name == "smartcourse_test"


async def test_init_mongo_registers_the_document_and_allows_insert(_mongo: None) -> None:
    note = await _Note(text="hello").insert()
    assert note.id is not None

    fetched = await _Note.get(note.id)
    assert fetched is not None
    assert fetched.text == "hello"
