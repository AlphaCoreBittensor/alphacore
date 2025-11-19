from alphacore.benchmark.core.evaluation.evaluator import Evaluator


def test_evaluator_uses_nested_spec_task_id(tmp_path, monkeypatch):
    """
    Regression test: ensure evaluator pulls task_id from nested task spec.
    """

    # prepare fake task and response
    task = {
        "task": {
            "task_id": "abc-123",
            "invariants": [],
        },
        "submit_requirements": {
            "bundle_layout": {
                "code_dir": "./",
                "state": "terraform.tfstate",
            }
        },
    }
    bundle_dir = tmp_path
    state_path = bundle_dir / "terraform.tfstate"
    state_path.write_text('{"resources": [{"type": "any", "instances": [{"attributes": {}}]}]}')

    def fake_run(cmd, cwd, capture_output, text, timeout, env):
        class Result:
            stdout = "No changes."

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    evaluator = Evaluator()
    score = evaluator.evaluate(task, {"bundle_dir": str(bundle_dir)})
    assert score.task_id == "abc-123"
