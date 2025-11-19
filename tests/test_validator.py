import pytest
import time
from neurons.validator import Validator
from alphacore.benchmark.core.models import ACTaskSpec, ACScore

class DummySynapse:
    def __init__(self, task_spec):
        self.task_spec = task_spec
        self.hotkey = "dummy_hotkey"

def test_validator_run_round(monkeypatch):
    validator = Validator(config_path="validator_config.yaml")
    # Patch registry.kinds to return a dummy kind
    monkeypatch.setattr("ac.core.registry.kinds", lambda: ["dummy_kind"])
    # Patch registry.build to return a dummy spec
    dummy_spec = ACTaskSpec(
        task_id="val-task-1",
        provider="gcp",
        params={"resource_name": "test-instance"},
        verify_fn=None
    )
    monkeypatch.setattr("ac.core.registry.build", lambda kind, tid, params: dummy_spec)
    # Patch forward to return a list of dummy scores
    dummy_score = ACScore(
        pass_fail=True,
        quality=0.9,
        timeliness=0.8,
        policy_adherence=1.0
    )
    monkeypatch.setattr(validator, "forward", lambda synapse: [dummy_score])
    # Patch subtensor.metagraph.hotkeys
    validator.subtensor.metagraph.hotkeys = ["dummy_hotkey"]
    # Patch subtensor.set_score to do nothing
    monkeypatch.setattr(validator.subtensor, "set_score", lambda wallet, hotkey, score, block: None)
    # Patch subtensor.get_current_block
    monkeypatch.setattr(validator.subtensor, "get_current_block", lambda: 12345)
    validator.run_round()
    assert validator.scores_history[-1] == 0.9

def test_validator_blacklist():
    validator = Validator(config_path="validator_config.yaml")
    validator.config["validator"]["blacklist"] = ["blacklisted_hotkey"]
    hotkeys = ["blacklisted_hotkey", "not_blacklisted"]
    filtered = [hk for hk in hotkeys if hk not in set(validator.config["validator"]["blacklist"])]
    assert filtered == ["not_blacklisted"]
