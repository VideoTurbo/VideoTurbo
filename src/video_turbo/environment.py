"""Read VideoTurbo settings from the environment."""

import os


def getenv(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)
