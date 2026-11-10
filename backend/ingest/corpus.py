"""Loading the corpus definition from config/."""

from pathlib import Path

import yaml

from app.config import ROOT_DIR

CONFIG_DIR = ROOT_DIR / "config"


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_sources(config_dir: Path = CONFIG_DIR) -> list[dict]:
    return load_yaml(config_dir / "sources.yaml").get("sources") or []
