"""Brings each deployable's .env up to date with its env.example: creates a missing .env
from the example, and appends any key the example has that the .env lacks. Never
changes or removes a line that is already there, so local values (credentials, ports)
are untouched. Run via `make env`.
"""

import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


def sync(directory: Path) -> str:
    example = directory / "env.example"
    target = directory / ".env"
    if not target.exists():
        target.write_text(example.read_text())
        return "created"
    present = set(dotenv_values(target))
    missing = [
        line
        for line in example.read_text().splitlines()
        if "=" in line
        and not line.lstrip().startswith("#")
        and line.split("=", 1)[0].strip() not in present
    ]
    if not missing:
        return "up to date"
    with target.open("a") as handle:
        handle.write("\n# --- added by `make env` from env.example ---\n")
        handle.write("\n".join(missing) + "\n")
    return f"added {len(missing)} key(s)"


def main(names: list[str]) -> None:
    for name in names:
        directory = ROOT / ("gateway" if name == "gateway" else f"services/{name}")
        print(f"{directory.relative_to(ROOT)}/.env: {sync(directory)}")


if __name__ == "__main__":
    main(sys.argv[1:])
