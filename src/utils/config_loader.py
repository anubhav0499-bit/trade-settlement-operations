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


def get_segment_settlement_config() -> dict:
    return load_config("segment_settlement.yaml")


def get_derivatives_settlement_config() -> dict:
    return load_config("derivatives_settlement.yaml")


def get_margin_framework_config() -> dict:
    return load_config("margin_framework.yaml")


def get_debt_settlement_config() -> dict:
    return load_config("debt_settlement.yaml")


def get_t0_settlement_config() -> dict:
    return load_config("t0_settlement.yaml")
