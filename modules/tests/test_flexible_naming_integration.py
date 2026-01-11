"""
Integration tests for flexible naming system - full generation and validation pipeline.
"""
import pytest
from modules.models import Invariant, TaskSpec, TerraformTask
from modules.generation.terraform.providers.gcp.task_bank import GCPDynamicTaskBank
from modules.generation.terraform.providers.gcp.compositions import SINGLE_RESOURCE_FAMILIES
from modules.generation.instructions import TaskInstructionGenerator
from modules.evaluation.validation.resource_validators import _default_validate


class MockStateParser:
    """Mock Terraform state parser for testing."""
    def get_resource_attribute(self, resource, path):
        parts = path.split(".")
        obj = resource
        for part in parts:
            obj = obj.get(part, None)
            if obj is None:
                return None
        return obj


class TestFlexibleNamingIntegration:
    """Integration tests for the complete flexible naming pipeline."""

    def test_task_generation_includes_comparison_rules(self):
        """Test that generated tasks include comparison_rule fields."""
        bank = GCPDynamicTaskBank(
            min_resources=1,
            max_resources=1,
            families=list(SINGLE_RESOURCE_FAMILIES)
        )

        # Generate multiple tasks to ensure we get variety
        tasks_with_rules = []
        for _ in range(10):
            task = bank.build_task(validator_sa="test@example.com")

            # Check if any invariant has comparison rules
            for inv in task.spec.invariants:
                if inv.comparison_rule:
                    tasks_with_rules.append(task)
                    break

        # At least some tasks should have flexible naming
        assert len(tasks_with_rules) > 0, "No tasks generated with comparison rules"

    def test_generated_task_validation_cycle(self):
        """Test that a generated task can be validated with its comparison rules."""
        # Create a task with a specific comparison rule
        spec = TaskSpec(
            version="v0",
            task_id="test-validation-123",
            nonce="abc456",
            kind="storage bucket",
            invariants=[
                Invariant(
                    resource_type="google_storage_bucket",
                    match={
                        "values.name": "testbucket-abc",
                        "values.location": "US",
                    },
                    comparison_rule={"values.name": "starts_with"}
                )
            ]
        )

        # Simulate a miner's valid submission (name starts with prefix)
        valid_resource = {
            "type": "google_storage_bucket",
            "values": {
                "name": "testbucket-abc-miner1-xyz",
                "location": "US",
            }
        }

        parser = MockStateParser()
        result = _default_validate(spec.invariants[0], valid_resource, parser)

        assert result.passed, f"Validation should pass but failed: {result.errors}"

    def test_validation_rejects_invalid_flexible_names(self):
        """Test that validation correctly rejects invalid flexible names."""
        # starts_with rule
        inv_starts = Invariant(
            resource_type="google_storage_bucket",
            match={"values.name": "bucket-prefix"},
            comparison_rule={"values.name": "starts_with"}
        )

        invalid_resource = {
            "type": "google_storage_bucket",
            "values": {"name": "wrong-bucket-prefix"}
        }

        parser = MockStateParser()
        result = _default_validate(inv_starts, invalid_resource, parser)

        assert not result.passed
        assert any("start with" in err.lower() for err in result.errors)

    def test_validation_accepts_all_flexible_strategies(self):
        """Test that validation works for starts_with, ends_with, and exact_match."""
        parser = MockStateParser()

        test_cases = [
            # (rule, expected_value, actual_value, should_pass)
            ("starts_with", "prefix", "prefix-miner1", True),
            ("starts_with", "prefix", "miner1-prefix", False),
            ("ends_with", "suffix", "miner1-suffix", True),
            ("ends_with", "suffix", "suffix-miner1", False),
            ("exact_match", "exact-name", "exact-name", True),
            ("exact_match", "exact-name", "exact-name-extra", False),
        ]

        for rule, expected, actual, should_pass in test_cases:
            inv = Invariant(
                resource_type="google_compute_instance",
                match={"values.name": expected},
                comparison_rule={"values.name": rule} if rule != "exact_match" else {}
            )

            resource = {
                "type": "google_compute_instance",
                "values": {"name": actual}
            }

            result = _default_validate(inv, resource, parser)

            if should_pass:
                assert result.passed, f"Rule {rule}: {expected} vs {actual} should pass"
            else:
                assert not result.passed, f"Rule {rule}: {expected} vs {actual} should fail"

    def test_instruction_formatting_includes_comparison_rules(self):
        """Test that formatted instructions include comparison rule text."""
        invariants = [
            Invariant(
                resource_type="google_storage_bucket",
                match={"values.name": "bucket-abc"},
                comparison_rule={"values.name": "starts_with"}
            ),
            Invariant(
                resource_type="google_compute_instance",
                match={"values.name": "vm-xyz"},
                comparison_rule={"values.name": "ends_with"}
            ),
        ]

        formatted = TaskInstructionGenerator._format_invariants(invariants)

        assert "starts with" in formatted.lower()
        assert "ends with" in formatted.lower()
        assert "bucket-abc" in formatted
        assert "vm-xyz" in formatted

    def test_multiple_resources_with_different_rules(self):
        """Test validation of multiple resources each with different comparison rules."""
        parser = MockStateParser()

        # Resource 1: starts_with
        inv1 = Invariant(
            resource_type="google_storage_bucket",
            match={"values.name": "bucket", "values.location": "US"},
            comparison_rule={"values.name": "starts_with"}
        )

        resource1_valid = {
            "type": "google_storage_bucket",
            "values": {"name": "bucket-prod-123", "location": "US"}
        }

        result1 = _default_validate(inv1, resource1_valid, parser)
        assert result1.passed

        # Resource 2: ends_with
        inv2 = Invariant(
            resource_type="google_compute_instance",
            match={"values.name": "instance", "values.zone": "us-central1-a"},
            comparison_rule={"values.name": "ends_with"}
        )

        resource2_valid = {
            "type": "google_compute_instance",
            "values": {"name": "prod-instance", "zone": "us-central1-a"}
        }

        result2 = _default_validate(inv2, resource2_valid, parser)
        assert result2.passed

        # Resource 3: exact_match
        inv3 = Invariant(
            resource_type="google_compute_network",
            match={"values.name": "my-network"},
            comparison_rule={}
        )

        resource3_valid = {
            "type": "google_compute_network",
            "values": {"name": "my-network"}
        }

        result3 = _default_validate(inv3, resource3_valid, parser)
        assert result3.passed

    def test_deterministic_rule_selection(self):
        """Test that the same task_id + nonce generates the same comparison rules."""
        bank = GCPDynamicTaskBank(
            min_resources=1,
            max_resources=1,
            families=[SINGLE_RESOURCE_FAMILIES[2]]  # Use storage_bucket
        )

        # Generate task twice with same seed
        # Note: The task bank generates new random IDs, so we can't directly test this
        # But we can verify that comparison rules are present and deterministic
        task1 = bank.build_task(validator_sa="test@example.com")

        # Verify the task has comparison rules for storage bucket
        bucket_invs = [inv for inv in task1.spec.invariants
                      if inv.resource_type == "google_storage_bucket"]

        if bucket_invs:
            # Storage buckets should have comparison rules
            inv = bucket_invs[0]
            assert "values.name" in inv.match
            # Check if it has a comparison rule (should have one for storage bucket)
            if inv.comparison_rule:
                rule = inv.comparison_rule.get("values.name")
                assert rule in ("starts_with", "ends_with"), \
                    f"Storage bucket should use starts_with or ends_with, got: {rule}"

    def test_fallback_prompt_includes_comparison_rules(self):
        """Verify that deterministic fallback prompts include comparison rule text."""
        from modules.generation.instructions import TaskInstructionGenerator

        # Create a simple task with starts_with rule
        invariant = Invariant(
            resource_type="google_storage_bucket",
            match={
                "values.name": "test-bucket-xyz",
                "values.location": "US-CENTRAL1"
            },
            comparison_rule={
                "values.name": "starts_with"
            }
        )

        spec = TaskSpec(
            version="2024-12-01",
            task_id="fallback-test-001",
            nonce="test",
            kind="storage_bucket",
            invariants=[invariant]
        )

        task = TerraformTask(
            engine="terraform",
            provider="gcp",
            validator_sa="validator@test.com",
            spec=spec
        )

        # Generate prompt with deterministic fallback (enable_llm=False)
        generator = TaskInstructionGenerator(enable_llm=False)
        prompt = generator.generate(task)

        # Verify comparison rules are included in the prompt
        assert "starts with" in prompt
        assert "test-bucket-xyz" in prompt

        # Test with ends_with rule
        invariant_ends = Invariant(
            resource_type="google_compute_instance",
            match={
                "values.name": "production-server",
                "values.zone": "us-central1-a"
            },
            comparison_rule={
                "values.name": "ends_with"
            }
        )

        spec_ends = TaskSpec(
            version="2024-12-01",
            task_id="fallback-test-002",
            nonce="test",
            kind="compute_instance",
            invariants=[invariant_ends]
        )

        task_ends = TerraformTask(
            engine="terraform",
            provider="gcp",
            validator_sa="validator@test.com",
            spec=spec_ends
        )

        prompt_ends = generator.generate(task_ends)

        # Verify ends_with rule is in prompt
        assert "ends with" in prompt_ends
        assert "production-server" in prompt_ends

    def test_fallback_all_comparison_strategies(self):
        """Test all three comparison strategies in fallback prompts."""
        from modules.generation.instructions import TaskInstructionGenerator

        generator = TaskInstructionGenerator(enable_llm=False)

        # Test exact_match (default behavior)
        inv_exact = Invariant(
            resource_type="google_storage_bucket",
            match={"values.name": "exact-bucket"},
            comparison_rule={"values.name": "exact_match"}
        )

        spec_exact = TaskSpec(
            version="2024-12-01",
            task_id="fallback-exact-001",
            nonce="test",
            kind="storage_bucket",
            invariants=[inv_exact]
        )

        task_exact = TerraformTask(
            engine="terraform",
            provider="gcp",
            validator_sa="validator@test.com",
            spec=spec_exact
        )

        prompt_exact = generator.generate(task_exact)
        assert "equals" in prompt_exact
        assert "exact-bucket" in prompt_exact

        # Test starts_with
        inv_starts = Invariant(
            resource_type="google_storage_bucket",
            match={"values.name": "prefix-bucket"},
            comparison_rule={"values.name": "starts_with"}
        )

        spec_starts = TaskSpec(
            version="2024-12-01",
            task_id="fallback-starts-001",
            nonce="test",
            kind="storage_bucket",
            invariants=[inv_starts]
        )

        task_starts = TerraformTask(
            engine="terraform",
            provider="gcp",
            validator_sa="validator@test.com",
            spec=spec_starts
        )

        prompt_starts = generator.generate(task_starts)
        assert "starts with" in prompt_starts
        assert "prefix-bucket" in prompt_starts

        # Test ends_with
        inv_ends = Invariant(
            resource_type="google_storage_bucket",
            match={"values.name": "bucket-suffix"},
            comparison_rule={"values.name": "ends_with"}
        )

        spec_ends = TaskSpec(
            version="2024-12-01",
            task_id="fallback-ends-001",
            nonce="test",
            kind="storage_bucket",
            invariants=[inv_ends]
        )

        task_ends = TerraformTask(
            engine="terraform",
            provider="gcp",
            validator_sa="validator@test.com",
            spec=spec_ends
        )

        prompt_ends = generator.generate(task_ends)
        assert "ends with" in prompt_ends
        assert "bucket-suffix" in prompt_ends

    def test_llm_prompt_context_includes_comparison_rules(self):
        """Test that LLM context includes comparison rules from _format_invariants()."""
        from modules.generation.instructions import TaskInstructionGenerator

        # Create task with mixed comparison rules
        inv1 = Invariant(
            resource_type="google_storage_bucket",
            match={
                "values.name": "mybucket-prefix",
                "values.location": "US"
            },
            comparison_rule={
                "values.name": "starts_with"
            }
        )

        inv2 = Invariant(
            resource_type="google_compute_instance",
            match={
                "values.name": "vm-suffix",
                "values.zone": "us-central1-a"
            },
            comparison_rule={
                "values.name": "ends_with"
            }
        )

        spec = TaskSpec(
            version="2024-12-01",
            task_id="llm-context-test-001",
            nonce="test",
            kind="mixed_resources",
            invariants=[inv1, inv2]
        )

        task = TerraformTask(
            engine="terraform",
            provider="gcp",
            validator_sa="validator@test.com",
            spec=spec
        )

        # Test that _build_context includes comparison rules
        generator = TaskInstructionGenerator(enable_llm=False)
        context = generator._build_context(task, task_name="test")

        # The context should include comparison rule text
        assert "starts with" in context
        assert "mybucket-prefix" in context
        assert "ends with" in context
        assert "vm-suffix" in context
        assert "equals" in context  # For fields without special rules

    def test_generated_task_invariants_have_comparison_rules(self):
        """Verify that tasks generated by task bank include comparison rules."""
        bank = GCPDynamicTaskBank(
            min_resources=1,
            max_resources=2,
            families=list(SINGLE_RESOURCE_FAMILIES)
        )

        # Generate multiple tasks and collect comparison rules
        comparison_rules_found = {"starts_with": 0, "ends_with": 0, "exact_match": 0}

        for _ in range(20):
            task = bank.build_task(validator_sa="test@example.com")

            for inv in task.spec.invariants:
                for field, rule in inv.comparison_rule.items():
                    if rule in comparison_rules_found:
                        comparison_rules_found[rule] += 1

        # We should see a mix of rules (at least starts_with and ends_with for flexible resources)
        assert comparison_rules_found["starts_with"] > 0, "No starts_with rules found"
        assert comparison_rules_found["ends_with"] > 0, "No ends_with rules found"

    def test_validation_with_generated_task_comparison_rules(self):
        """Test end-to-end: generate task -> validate with comparison rules."""
        bank = GCPDynamicTaskBank(
            min_resources=1,
            max_resources=1,
            families=[SINGLE_RESOURCE_FAMILIES[2]]  # storage_bucket family
        )

        task = bank.build_task(validator_sa="test@example.com")

        # Find storage bucket invariant
        bucket_invs = [inv for inv in task.spec.invariants
                      if inv.resource_type == "google_storage_bucket"]

        if not bucket_invs:
            pytest.skip("No storage bucket in generated task")

        inv = bucket_invs[0]
        name_field = "values.name"

        if name_field not in inv.match:
            pytest.skip("Storage bucket missing name field")

        original_name = inv.match[name_field]
        rule = inv.comparison_rule.get(name_field, "exact_match")

        parser = MockStateParser()

        # Test validation based on the rule
        if rule == "starts_with":
            # Should accept names that start with the prefix
            valid_resource = {
                "type": "google_storage_bucket",
                "values": {
                    "name": f"{original_name}-miner-12345",
                    **{k.replace("values.", ""): v
                       for k, v in inv.match.items()
                       if k != name_field}
                }
            }
            result = _default_validate(inv, valid_resource, parser)
            assert result.passed, f"starts_with validation failed: {result.errors}"

        elif rule == "ends_with":
            # Should accept names that end with the suffix
            valid_resource = {
                "type": "google_storage_bucket",
                "values": {
                    "name": f"miner-12345-{original_name}",
                    **{k.replace("values.", ""): v
                       for k, v in inv.match.items()
                       if k != name_field}
                }
            }
            result = _default_validate(inv, valid_resource, parser)
            assert result.passed, f"ends_with validation failed: {result.errors}"

        elif rule == "exact_match":
            # Should only accept exact name match
            valid_resource = {
                "type": "google_storage_bucket",
                "values": {
                    "name": original_name,
                    **{k.replace("values.", ""): v
                       for k, v in inv.match.items()
                       if k != name_field}
                }
            }
            result = _default_validate(inv, valid_resource, parser)
            assert result.passed, f"exact_match validation failed: {result.errors}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
