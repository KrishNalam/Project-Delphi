"""Loads .env secrets and profile.yaml. Everything else imports from here."""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).parent

for line in (ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else []:
    key, sep, value = line.partition("=")
    if sep and not key.strip().startswith("#"):
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

PROFILE = yaml.safe_load((ROOT / "profile.yaml").read_text()) if (ROOT / "profile.yaml").exists() else {}


def env(key, default=None):
    return os.environ.get(key) or default
