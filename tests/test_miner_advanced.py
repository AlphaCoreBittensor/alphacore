import pytest
import threading
import asyncio
from neurons.miner import Miner
from alphacore.benchmark.core.models import ACTaskSpec

class DummySynapse:
    def __init__(self, task_spec, hotkey="dummy_hotkey"):
        self.task_spec = task_spec
        self.hotkey = hotkey

def test_miner_forward_exception(monkeypatch):
    miner = Miner(config_path="miner_config.yaml")
    # Patch _credentials_valid to raise exception
    monkeypatch.setattr(miner, "_credentials_valid", lambda provider, creds: (_ for _ in ()).throw(Exception("Test exception")))
    spec = ACTaskSpec(task_id="ex-task", provider="gcp", params={}, verify_fn=None)
    synapse = DummySynapse(spec)
    result = asyncio.run(miner.forward(synapse))
    assert result.status == "error"

def test_miner_thread_management():
    miner = Miner(config_path="miner_config.yaml")
    miner.run_in_background_thread()
    assert miner.is_running
    miner.stop_run_thread()
    assert not miner.is_running

def test_miner_context_manager():
    with Miner(config_path="miner_config.yaml") as miner:
        assert miner.is_running
    assert not miner.is_running

def test_miner_resync_metagraph(monkeypatch):
    miner = Miner(config_path="miner_config.yaml")
    called = {}
    def fake_sync(subtensor=None):
        called["sync"] = True
    monkeypatch.setattr(miner.metagraph, "sync", fake_sync)
    miner.resync_metagraph()
    assert called["sync"]

def test_miner_credential_validation_all_providers():
    miner = Miner(config_path="miner_config.yaml")
    providers = ["aws", "azure", "gcp"]
    for provider in providers:
        creds = {"key": "value"}
        assert miner._credentials_valid(provider, creds)
        assert not miner._credentials_valid(provider, {})
