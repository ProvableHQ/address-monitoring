"""Read secret configuration without executing .env contents."""

import os
from pathlib import Path
import shlex

ENV_FILE = Path(__file__).resolve().parent / ".env"


def secret_value(name, env_file=ENV_FILE):
    value = os.environ.get(name)
    if value is None and Path(env_file).is_file():
        for line in Path(env_file).read_text().splitlines():
            key, separator, raw = line.strip().removeprefix("export ").partition("=")
            if separator and key.strip() == name:
                try:
                    tokens = shlex.split(raw, comments=True)
                except ValueError:
                    raise ValueError(f"Invalid {name} entry in .env") from None
                if len(tokens) > 1:
                    raise ValueError(f"Invalid {name} entry in .env")
                value = tokens[0] if tokens else ""
    if value is not None and any(c in value for c in "\r\n"):
        raise ValueError(f"Invalid {name} value")
    return value.strip() if value else None
