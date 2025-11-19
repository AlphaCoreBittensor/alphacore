import pytest
import time
from neurons.miner import Miner
from alphacore.benchmark.core.models import ACTaskSpec

class DummySynapse:
    def __init__(self, task_spec):
        self.task_spec = task_spec
        self.hotkey = "dummy_hotkey"

def test_miner_forward_success():
    miner = Miner(config_path="miner_config.yaml")
    spec = ACTaskSpec(
        task_id="test-task-1",
        provider="gcp",
        params={"resource_name": "test-instance"},
        verify_fn=None
    )
    synapse = DummySynapse(spec)
    result = pytest.run(miner.forward(synapse))
    assert result.status == "success"
    assert result.task_id == "test-task-1"
    assert "resource_name" in result.resource_identifiers

def test_miner_forward_invalid_credentials():
    miner = Miner(config_path="miner_config.yaml")
    spec = ACTaskSpec(
        task_id="test-task-2",
        provider="aws",
        params={"resource_name": "test-instance"},
        verify_fn=None
    )
    # Remove credentials for AWS to simulate invalid
    miner.cloud_credentials["aws"] = {}
    synapse = DummySynapse(spec)
    result = pytest.run(miner.forward(synapse))
    assert result.status == "error"

def test_miner_blacklist():
    miner = Miner(config_path="miner_config.yaml")
    miner.config["miner"]["blacklist"] = ["blacklisted_hotkey"]
    synapse = DummySynapse(None)
    synapse.hotkey = "blacklisted_hotkey"
    assert miner.blacklist(synapse) is True
    synapse.hotkey = "not_blacklisted"
    assert miner.blacklist(synapse) is False

def test_miner_priority():
    miner = Miner(config_path="miner_config.yaml")
    synapse = DummySynapse(None)
    assert miner.priority(synapse) == 0
