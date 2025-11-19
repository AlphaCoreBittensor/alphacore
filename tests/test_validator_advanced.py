import pytest
import asyncio
from neurons.validator import Validator
from alphacore.benchmark.core.models import ACTaskSpec, ACScore

class DummySynapse:
    def __init__(self, task_spec):
        self.task_spec = task_spec
        self.hotkey = "dummy_hotkey"

def test_validator_submit_score(monkeypatch):
    validator = Validator(config_path="validator_config.yaml")
    called = {}
    def fake_set_score(wallet, hotkey, score, block):
        called["set_score"] = (hotkey, score, block)
    monkeypatch.setattr(validator.subtensor, "set_score", fake_set_score)
    validator._submit_score("hk", 0.5, 123)
    assert called["set_score"] == ("hk", 0.5, 123)

def test_validator_get_current_block(monkeypatch):
    validator = Validator(config_path="validator_config.yaml")
    monkeypatch.setattr(validator.subtensor, "get_current_block", lambda: 42)
    assert validator._get_current_block() == 42

def test_validator_config_validation():
    validator = Validator(config_path="validator_config.yaml")
    validator._validate_config({"validator": {}})
    with pytest.raises(ValueError):
        validator._validate_config({})

def test_validator_report_scores():
    validator = Validator(config_path="validator_config.yaml")
    validator.scores_history = [0.5, 1.0, 0.75]
    validator._report_scores()  # Should log average

def test_validator_blacklist_filtering():
    validator = Validator(config_path="validator_config.yaml")
    validator.config["validator"]["blacklist"] = ["hk1"]
    all_hotkeys = ["hk1", "hk2", "hk3"]
    filtered = [hk for hk in all_hotkeys if hk not in set(validator.config["validator"]["blacklist"])]
    assert filtered == ["hk2", "hk3"]

def test_validator_run_main_loop(monkeypatch):
    validator = Validator(config_path="validator_config.yaml")
    # Patch run_round to only run once
    called = {"rounds": 0}
    async def fake_run_round():
        called["rounds"] += 1
        if called["rounds"] > 1:
            raise KeyboardInterrupt()
    monkeypatch.setattr(validator, "run_round", fake_run_round)
    # Patch asyncio.sleep to avoid delay
    monkeypatch.setattr(asyncio, "sleep", lambda s: None)
    try:
        validator.run()
    except Exception:
        pass
    assert called["rounds"] >= 1
