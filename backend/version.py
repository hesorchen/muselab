"""Project version resolved from the packaging source of truth."""

from __future__ import annotations

import tomllib
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


@lru_cache(maxsize=1)
def project_version() -> str:
    try:
        return version("muselab")
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        try:
            value = tomllib.loads(
                pyproject.read_text(encoding="utf-8"))
            return str(value["project"]["version"])
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
            return "0.0.0"
