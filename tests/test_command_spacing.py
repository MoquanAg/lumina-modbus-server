"""Tests for per-baud command spacing and optional per-device overrides.

The override mechanism lets a device tune spacing via an untracked
spacing_overrides.json (e.g. hengyuan's ttyAMA1 reflection workaround) instead
of editing calculate_min_command_spacing() in tracked code.
"""

import os
import json
import tempfile

from main import calculate_min_command_spacing, _load_spacing_overrides


def test_default_spacing_by_baud():
    """Stock per-baud defaults are unchanged when no override applies."""
    assert calculate_min_command_spacing(4800) == 0.25
    assert calculate_min_command_spacing(9600) == 0.20
    assert calculate_min_command_spacing(38400) == 0.08
    assert calculate_min_command_spacing(115200) == 0.05


def test_override_replaces_default_for_that_baud():
    """A per-device override bumps spacing without touching code."""
    assert calculate_min_command_spacing(9600, overrides={9600: 0.50}) == 0.50


def test_override_does_not_affect_other_bauds():
    """An override for one baud must not change other baud rates."""
    assert calculate_min_command_spacing(38400, overrides={9600: 0.50}) == 0.08


def test_load_spacing_overrides_parses_json_file():
    """_load_spacing_overrides reads {baud: seconds} from a JSON file."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "spacing_overrides.json")
    with open(path, "w") as f:
        json.dump({"9600": 0.5}, f)
    assert _load_spacing_overrides(path) == {9600: 0.5}


def test_load_spacing_overrides_missing_file_is_empty():
    """A missing/invalid overrides file yields no overrides (stock behavior)."""
    assert _load_spacing_overrides("/nonexistent/spacing_overrides.json") == {}
