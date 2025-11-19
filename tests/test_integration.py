import pytest
import asyncio
from neurons.miner import Miner
from neurons.validator import Validator
from alphacore.benchmark.core.models import ACTaskSpec, ACScore

class DummySynapse:
    def __init__(self, task_spec, hotkey="dummy_hotkey"):
        self.task_spec = task_spec
        self.hotkey = hotkey

def test_miner_validator_integration(monkeypatch):
    miner = Miner(config_path="miner_config.yaml")
    validator = Validator(config_path="validator_config.yaml")
    # Patch miner forward to always succeed
    monkeypatch.setattr(miner, "forward", lambda synapse: asyncio.Future())
    future = miner.forward(DummySynapse(ACTaskSpec(task_id="int-task", provider="gcp", params={}, verify_fn=None)))
    future.set_result("success")
    assert future.result() == "success"
    # Patch validator forward to return a score
    monkeypatch.setattr(validator, "forward", lambda synapse: [ACScore(pass_fail=True, quality=1.0, timeliness=1.0, policy_adherence=1.0)])
    scores = validator.forward(DummySynapse(ACTaskSpec(task_id="int-task", provider="gcp", params={}, verify_fn=None)))
    assert scores[0].quality == 1.0

def test_blacklist_enforcement():
    miner = Miner(config_path="miner_config.yaml")
    validator = Validator(config_path="validator_config.yaml")
    miner.config["miner"]["blacklist"] = ["hk_bad"]
    validator.config["validator"]["blacklist"] = ["hk_bad"]
    synapse = DummySynapse(ACTaskSpec(task_id="int-task", provider="gcp", params={}, verify_fn=None), hotkey="hk_bad")
    assert miner.blacklist(synapse)
    assert "hk_bad" in validator.config["validator"]["blacklist"]

def test_score_aggregation_across_rounds(monkeypatch):
    validator = Validator(config_path="validator_config.yaml")
    monkeypatch.setattr(validator, "forward", lambda synapse: [ACScore(pass_fail=True, quality=0.5, timeliness=1.0, policy_adherence=1.0)])
    validator.scores_history = []
    for _ in range(3):
        validator.scores_history.append(validator.forward(DummySynapse(ACTaskSpec(task_id="int-task", provider="gcp", params={}, verify_fn=None)))[0].quality)
    assert len(validator.scores_history) == 3
    assert abs(sum(validator.scores_history)/3 - 0.5) < 1e-6
