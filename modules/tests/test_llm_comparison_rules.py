"""
Tests for LLM prompt generation with comparison rules.

These tests verify that LLM-generated prompts include comparison rule qualifiers
like 'starts with', 'ends with', and 'equals' when present in task invariants.
"""
import pytest
from modules.models import Invariant, TaskSpec, TerraformTask
from modules.generation.instructions import TaskInstructionGenerator


class TestLLMComparisonRulePreservation:
    """Test that LLM system prompt correctly instructs preservation of comparison rules."""

    def test_llm_context_includes_comparison_rules(self):
        """Verify that _build_context() includes comparison rule text for LLM."""
        invariant = Invariant(
            resource_type="google_storage_bucket",
            match={
                "values.name": "mybucket-prefix",
                "values.location": "US"
            },
            comparison_rule={
                "values.name": "starts_with"
            }
        )

        spec = TaskSpec(
            version="v0",
            task_id="test-llm-context-001",
            nonce="test",
            kind="storage_bucket",
            invariants=[invariant]
        )

        task = TerraformTask(
            engine="terraform",
            provider="gcp",
            validator_sa="validator@example.com",
            spec=spec
        )

        generator = TaskInstructionGenerator(enable_llm=False)
        context = generator._build_context(task, task_name="test")

        # Context should include comparison rule text
        assert "starts with" in context
        assert "mybucket-prefix" in context
        # Other fields should use equals
        assert "equals" in context

    def test_llm_system_prompt_includes_comparison_rule_instruction(self):
        """Verify that system prompt instructs LLM to preserve comparison qualifiers."""
        # The system prompt is constructed in generate() method
        # We can't easily test the exact system prompt without mocking,
        # but we can verify the instruction is in the source code

        from modules.generation.instructions import TaskInstructionGenerator
        import inspect

        source = inspect.getsource(TaskInstructionGenerator.generate)

        # Check that the system prompt includes the critical instruction
        assert "CRITICAL: When resource requirements specify flexible naming rules" in source
        assert "you MUST preserve these comparison qualifiers" in source
        assert "'starts with', 'ends with', or 'equals'" in source

    def test_fallback_includes_ends_with_for_vm_name(self):
        """
        Test the specific case from user report:
        VM with ends_with rule should generate prompt with 'ends with' text.
        """
        invariant = Invariant(
            resource_type="google_compute_instance",
            match={
                "values.name": "vm-ade66267",
                "values.zone": "europe-west1-b",
                "values.machine_type": "e2-medium"
            },
            comparison_rule={
                "values.name": "ends_with"
            }
        )

        spec = TaskSpec(
            version="v0",
            task_id="test-vm-ends-with",
            nonce="test",
            kind="basic virtual machine",
            invariants=[invariant]
        )

        task = TerraformTask(
            engine="terraform",
            provider="gcp",
            validator_sa="validator@example.com",
            spec=spec
        )

        # Generate with fallback
        generator = TaskInstructionGenerator(enable_llm=False)
        prompt = generator.generate(task)

        # Prompt must include 'ends with' to inform miners
        assert "ends with" in prompt.lower()
        assert "vm-ade66267" in prompt

        # Should NOT say "name it vm-ade66267" or "name is vm-ade66267"
        # which would imply exact match
        # It should say "name ends with vm-ade66267"

    def test_fallback_all_three_comparison_rules(self):
        """Test that fallback prompts correctly represent all three comparison rules."""
        # Test exact_match
        inv_exact = Invariant(
            resource_type="google_storage_bucket",
            match={"values.name": "exact-bucket"},
            comparison_rule={"values.name": "exact_match"}
        )

        spec_exact = TaskSpec(
            version="v0",
            task_id="test-exact",
            nonce="test",
            kind="storage",
            invariants=[inv_exact]
        )

        task_exact = TerraformTask(
            engine="terraform",
            provider="gcp",
            validator_sa="test@example.com",
            spec=spec_exact
        )

        generator = TaskInstructionGenerator(enable_llm=False)
        prompt_exact = generator.generate(task_exact)

        assert "equals" in prompt_exact.lower()
        assert "exact-bucket" in prompt_exact

        # Test starts_with
        inv_starts = Invariant(
            resource_type="google_storage_bucket",
            match={"values.name": "bucket-prefix"},
            comparison_rule={"values.name": "starts_with"}
        )

        spec_starts = TaskSpec(
            version="v0",
            task_id="test-starts",
            nonce="test",
            kind="storage",
            invariants=[inv_starts]
        )

        task_starts = TerraformTask(
            engine="terraform",
            provider="gcp",
            validator_sa="test@example.com",
            spec=spec_starts
        )

        prompt_starts = generator.generate(task_starts)

        assert "starts with" in prompt_starts.lower()
        assert "bucket-prefix" in prompt_starts

        # Test ends_with
        inv_ends = Invariant(
            resource_type="google_storage_bucket",
            match={"values.name": "bucket-suffix"},
            comparison_rule={"values.name": "ends_with"}
        )

        spec_ends = TaskSpec(
            version="v0",
            task_id="test-ends",
            nonce="test",
            kind="storage",
            invariants=[inv_ends]
        )

        task_ends = TerraformTask(
            engine="terraform",
            provider="gcp",
            validator_sa="test@example.com",
            spec=spec_ends
        )

        prompt_ends = generator.generate(task_ends)

        assert "ends with" in prompt_ends.lower()
        assert "bucket-suffix" in prompt_ends

    def test_context_with_mixed_comparison_rules(self):
        """Test that context correctly represents multiple different comparison rules."""
        inv1 = Invariant(
            resource_type="google_storage_bucket",
            match={
                "values.name": "bucket-start",
                "values.location": "US"
            },
            comparison_rule={
                "values.name": "starts_with"
            }
        )

        inv2 = Invariant(
            resource_type="google_compute_instance",
            match={
                "values.name": "vm-end",
                "values.zone": "us-central1-a"
            },
            comparison_rule={
                "values.name": "ends_with"
            }
        )

        spec = TaskSpec(
            version="v0",
            task_id="test-mixed",
            nonce="test",
            kind="mixed",
            invariants=[inv1, inv2]
        )

        task = TerraformTask(
            engine="terraform",
            provider="gcp",
            validator_sa="test@example.com",
            spec=spec
        )

        generator = TaskInstructionGenerator(enable_llm=False)
        context = generator._build_context(task, task_name="test")

        # Context should include both starts_with and ends_with
        assert "starts with" in context.lower()
        assert "bucket-start" in context
        assert "ends with" in context.lower()
        assert "vm-end" in context
        # Fields without special rules should use equals
        assert "equals" in context.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
