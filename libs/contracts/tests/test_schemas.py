"""BACKWARD compatibility for every registered schema (docs/adr/0012).

A consumer built against the committed schema must still be able to read what the
producer sends after a change. Within a major version that allows exactly one kind of
change: adding an optional field. Removing a field, renaming one, changing its type or
making it required is a new major version (a new model and topic).

When a compatible change is intended, regenerate the snapshots and commit them:

    UPDATE_SCHEMAS=1 pytest libs/contracts
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from smartcourse_contracts.registry import TOPIC_SCHEMAS

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


def _models() -> dict[str, type[BaseModel]]:
    return {model.__name__: model for model in TOPIC_SCHEMAS.values()}


def _committed(name: str) -> dict[str, Any]:
    path = SCHEMA_DIR / f"{name}.json"
    if os.environ.get("UPDATE_SCHEMAS"):
        path.write_text(json.dumps(_models()[name].model_json_schema(), indent=2) + "\n")
    if not path.exists():
        pytest.fail(f"{path.name} is not committed — run UPDATE_SCHEMAS=1 pytest")
    loaded: dict[str, Any] = json.loads(path.read_text())
    return loaded


def _incompatibilities(old: dict[str, Any], new: dict[str, Any], where: str) -> list[str]:
    """What an old reader would trip over in a new message, for one object schema."""
    problems: list[str] = []
    old_props: dict[str, Any] = old.get("properties", {})
    new_props: dict[str, Any] = new.get("properties", {})
    for field, old_field in old_props.items():
        if field not in new_props:
            problems.append(f"{where}.{field} was removed")
            continue
        if _shape(old_field) != _shape(new_props[field]):
            problems.append(f"{where}.{field} changed type")
    added_required = set(new.get("required", [])) - set(old.get("required", []))
    problems.extend(f"{where}.{field} became required" for field in sorted(added_required))
    return problems


def _shape(field: dict[str, Any]) -> Any:
    """The parts of a field schema that decide whether a value parses — not its title,
    description or default."""
    return {k: v for k, v in field.items() if k not in {"title", "description", "default"}}


@pytest.mark.parametrize("name", sorted(_models()))
def test_schema_is_backward_compatible(name: str) -> None:
    old = _committed(name)
    new = _models()[name].model_json_schema()

    problems = _incompatibilities(old, new, name)
    for def_name, old_def in old.get("$defs", {}).items():
        new_def = new.get("$defs", {}).get(def_name)
        if new_def is None:
            problems.append(f"{def_name} was removed")
        else:
            problems.extend(_incompatibilities(old_def, new_def, def_name))
    assert not problems, "Not BACKWARD compatible — publish a v2 instead:\n" + "\n".join(problems)


@pytest.mark.parametrize("name", sorted(_models()))
def test_committed_schema_is_current(name: str) -> None:
    """A compatible change still has to be committed, or the next change is checked
    against a stale baseline."""
    assert _committed(name) == _models()[name].model_json_schema(), (
        f"schemas/{name}.json is stale — run UPDATE_SCHEMAS=1 pytest and commit it"
    )


def test_every_topic_follows_the_naming_convention() -> None:
    for topic in TOPIC_SCHEMAS:
        parts = topic.split(".")
        assert parts[0] == "smartcourse" and parts[-1].startswith("v"), topic
