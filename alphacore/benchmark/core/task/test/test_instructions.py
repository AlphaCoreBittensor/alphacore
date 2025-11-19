import pytest

from alphacore.benchmark.core.task import TaskGenerationPipeline, TaskInstructionGenerator
from alphacore.benchmark.core.task.terraform.registry import (
    TerraformTaskRegistry,
    terraform_task_registry,
)
from alphacore.benchmark.core.task.terraform.providers.gcp.vm import build_task


@pytest.fixture(autouse=True)
def disable_llm_default():
    original = terraform_task_registry.default_instruction_generator
    terraform_task_registry.default_instruction_generator = TaskInstructionGenerator(
        enable_llm=False
    )
    yield
    terraform_task_registry.default_instruction_generator = original


def test_instruction_generator_fallback_mentions_requirements(monkeypatch):
    generator = TaskInstructionGenerator(enable_llm=False)
    task = build_task(validator_sa="validator@example.com")
    text = generator.generate(task, task_name="vm")
    assert task.spec.prompt is None
    assert "terraform" in text.lower()
    assert "invariant" not in text.lower()
    assert "→" not in text
    assert "*" not in text
    assert "`" not in text
    assert "readme" not in text.lower()
    assert "archive" in text.lower()
    assert "synapse" not in text.lower()
    assert task.spec.task_id not in text
    assert task.spec.kind not in text
    assert "repository root" in text
    invariant_name = task.spec.invariants[0].match["values.name"]
    assert invariant_name in text
    assert "terraform.tfstate" in text
    assert (
        TaskInstructionGenerator._describe_kind(task.spec.kind).lower()
        in text.lower()
    )
    assert task.spec.invariants[0].resource_type not in text


def test_registry_can_apply_instructions():
    class StubGenerator:
        def generate(self, task, task_name=None):
            return f"instructions for {task_name}"

    task = terraform_task_registry.build_random_task(
        provider="gcp",
        validator_sa="validator@example.com",
        instruction_generator=StubGenerator(),
    )
    assert task.instructions is not None
    assert task.spec.prompt == task.instructions


def test_registry_build_random_task_includes_prompt_by_default(monkeypatch):
    init_calls = {"count": 0}

    class StubGenerator:
        def __init__(self):
            init_calls["count"] += 1

        def generate(self, task, task_name=None):
            return f"default instructions for {task_name}"

    monkeypatch.setattr(
        "alphacore.benchmark.core.task.terraform.registry.TaskInstructionGenerator",
        StubGenerator,
    )

    registry = TerraformTaskRegistry()
    task = registry.build_random_task(
        provider="gcp",
        validator_sa="validator@example.com",
    )
    assert init_calls["count"] == 1
    assert task.instructions
    assert task.spec.prompt == task.instructions


def test_registry_direct_builder_includes_prompt():
    registry = TerraformTaskRegistry()
    builders = registry.get_task_builders("gcp")
    builder = builders.get("vm")
    assert builder is not None

    task = builder(validator_sa="validator@example.com")
    assert task.instructions
    assert task.spec.prompt == task.instructions


def test_task_generation_pipeline_includes_prompt():
    pipeline = TaskGenerationPipeline(
        validator_sa="validator@example.com",
        instruction_generator=TaskInstructionGenerator(enable_llm=False),
    )
    spec = pipeline.generate()
    assert spec.prompt
    assert "submit_requirements" in spec.params
    assert "task" in spec.params
    assert "prompt" not in spec.params
    assert spec.params["task"]["prompt"] == spec.prompt
    assert "→" not in spec.prompt
    assert "*" not in spec.prompt
    assert "archive" in spec.prompt.lower()
    assert "synapse" not in spec.prompt.lower()
    assert spec.kind not in spec.prompt
    assert spec.task_id not in spec.prompt
    assert "repository root" in spec.prompt


def test_plain_text_sanitizer_removes_markdown():
    result = TaskInstructionGenerator._to_plain_text(
        "**Bold**\n- item one\n1. item two\n`code`"
    )
    assert result == "Bold\nitem one\nitem two\ncode"


def test_invalid_llm_output_triggers_validation_error():
    generator = TaskInstructionGenerator(enable_llm=False)
    task = build_task(validator_sa="validator@example.com")
    with pytest.raises(ValueError):
        generator._enforce_allowed_content("Please add a README file", task)


def test_missing_provider_terms_raise():
    generator = TaskInstructionGenerator(enable_llm=False)
    task = build_task(validator_sa="validator@example.com")
    invariant = task.spec.invariants[0]
    fields = " ".join(str(value) for value in invariant.match.values())
    base = f"archive terraform.tfstate {fields}"
    with pytest.raises(ValueError):
        generator._enforce_allowed_content(base, task)


def test_terraform_task_to_dict_copies_prompt_into_spec():
    task = build_task(validator_sa="validator@example.com")
    task.instructions = "custom instructions"
    task.spec.prompt = None

    payload = task.to_dict()
    assert "prompt" not in payload
    assert payload["task"]["prompt"] == "custom instructions"
    assert "task_id" not in payload
    assert "kind" not in payload
    submit_requirements = payload["submit_requirements"]
    assert submit_requirements["code"] == "."
    assert submit_requirements["state"] == "terraform.tfstate"
    assert submit_requirements["package_format"] == "archive"


def test_instruction_generator_falls_back_when_llm_unavailable():
    generator = TaskInstructionGenerator(enable_llm=True)
    task = build_task(validator_sa="validator@example.com")
    text = generator.generate(task, task_name="vm")
    assert "terraform" in text.lower()
