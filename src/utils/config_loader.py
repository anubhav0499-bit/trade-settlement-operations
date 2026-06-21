"""Load YAML configuration files."""

from pathlib import Path

import yaml


CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


def load_config(filename: str) -> dict:
    filepath = CONFIG_DIR / filename
    with open(filepath, "r") as f:
        return yaml.safe_load(f)


def get_escalation_config() -> dict:
    return load_config("escalation_matrix.yaml")


def get_matching_config() -> dict:
    return load_config("matching_tolerances.yaml")


def get_confirmation_config() -> dict:
    return load_config("confirmation_cutoffs.yaml")
