"""
Tests for flexible naming validation system with starts_with, ends_with, and exact_match.
"""
import pytest
from modules.models import Invariant
from modules.generation.terraform.resource_templates import (
    ResourceTemplate,
    ResourceInstance,
    TemplateContext,
    pick_naming_rule,
)
from modules.evaluation.validation.resource_validators import _apply_comparison_rule
import random


class TestComparisonRules:
    """Test the comparison rule application logic."""

    def test_exact_match_success(self):
        """Test exact_match rule with matching values."""
        assert _apply_comparison_rule("my-bucket-123", "my-bucket-123", "exact_match")
        assert _apply_comparison_rule("vm-test", "vm-test", "exact_match")

    def test_exact_match_failure(self):
        """Test exact_match rule with non-matching values."""
        assert not _apply_comparison_rule("my-bucket-456", "my-bucket-123", "exact_match")
        assert not _apply_comparison_rule("vm-test-prod", "vm-test", "exact_match")

    def test_starts_with_success(self):
        """Test starts_with rule with matching prefix."""
        assert _apply_comparison_rule("my-bucket-user1-xyz", "my-bucket", "starts_with")
        assert _apply_comparison_rule("vm-test-production", "vm-test", "starts_with")
        assert _apply_comparison_rule("my-bucket", "my-bucket", "starts_with")

    def test_starts_with_failure(self):
        """Test starts_with rule with non-matching prefix."""
        assert not _apply_comparison_rule("user1-my-bucket", "my-bucket", "starts_with")
        assert not _apply_comparison_rule("test-vm", "vm-test", "starts_with")

    def test_ends_with_success(self):
        """Test ends_with rule with matching suffix."""
        assert _apply_comparison_rule("user1-my-bucket", "my-bucket", "ends_with")
        assert _apply_comparison_rule("production-vm-test", "vm-test", "ends_with")
        assert _apply_comparison_rule("my-bucket", "my-bucket", "ends_with")

    def test_ends_with_failure(self):
        """Test ends_with rule with non-matching suffix."""
        assert not _apply_comparison_rule("my-bucket-xyz", "my-bucket", "ends_with")
        assert not _apply_comparison_rule("vm-test-prod", "vm-test", "ends_with")

    def test_none_values(self):
        """Test comparison rules with None values."""
        assert _apply_comparison_rule(None, None, "exact_match")
        assert not _apply_comparison_rule(None, "value", "exact_match")
        assert not _apply_comparison_rule("value", None, "starts_with")


class TestInvariantModel:
    """Test the enhanced Invariant model."""

    def test_invariant_with_comparison_rules(self):
        """Test creating invariants with comparison rules."""
        inv = Invariant(
            resource_type="google_storage_bucket",
            match={
                "values.name": "my-bucket",
                "values.location": "US",
            },
            comparison_rule={
                "values.name": "starts_with",
            },
        )

        assert inv.comparison_rule["values.name"] == "starts_with"
        assert "values.location" not in inv.comparison_rule  # Should use default exact_match

    def test_invariant_default_comparison_rules(self):
        """Test invariants default to empty comparison rules."""
        inv = Invariant(
            resource_type="google_compute_instance",
            match={
                "values.name": "my-vm",
                "values.zone": "us-central1-a",
            },
        )

        assert inv.comparison_rule == {}
        assert len(inv.match) == 2


class TestResourceTemplateNamingRules:
    """Test resource template naming rules configuration."""

    def test_template_with_naming_rules(self):
        """Test creating templates with naming rules."""
        def dummy_builder(ctx):
            return ResourceInstance(invariants=[], prompt_hints=[])

        template = ResourceTemplate(
            key="test_bucket",
            kind="storage bucket",
            provides=("bucket",),
            builder=dummy_builder,
            naming_rules=("starts_with", "ends_with"),
        )

        assert template.naming_rules == ("starts_with", "ends_with")
        assert "exact_match" not in template.naming_rules

    def test_template_default_naming_rules(self):
        """Test templates default to exact_match."""
        def dummy_builder(ctx):
            return ResourceInstance(invariants=[], prompt_hints=[])

        template = ResourceTemplate(
            key="test_vm",
            kind="virtual machine",
            provides=("instance",),
            builder=dummy_builder,
        )

        assert template.naming_rules == ("exact_match",)

    def test_pick_naming_rule(self):
        """Test picking random naming rules from template."""
        def dummy_builder(ctx):
            return ResourceInstance(invariants=[], prompt_hints=[])

        template = ResourceTemplate(
            key="test_bucket",
            kind="storage bucket",
            provides=("bucket",),
            builder=dummy_builder,
            naming_rules=("starts_with", "ends_with"),
        )

        # Use deterministic RNG
        rng = random.Random(42)
        rule = pick_naming_rule(template, rng)

        assert rule in ("starts_with", "ends_with")

    def test_pick_naming_rule_deterministic(self):
        """Test that picking naming rules is deterministic with same seed."""
        def dummy_builder(ctx):
            return ResourceInstance(invariants=[], prompt_hints=[])

        template = ResourceTemplate(
            key="test_bucket",
            kind="storage bucket",
            provides=("bucket",),
            builder=dummy_builder,
            naming_rules=("starts_with", "ends_with", "exact_match"),
        )

        rng1 = random.Random(12345)
        rng2 = random.Random(12345)

        rule1 = pick_naming_rule(template, rng1)
        rule2 = pick_naming_rule(template, rng2)

        assert rule1 == rule2  # Deterministic


class TestValidationIntegration:
    """Integration tests for validation with comparison rules."""

    def test_validate_storage_bucket_starts_with(self):
        """Test validating a storage bucket with starts_with rule."""
        from modules.evaluation.validation.resource_validators import _default_validate
        from modules.evaluation.validation.state_parser import TerraformStateParser

        inv = Invariant(
            resource_type="google_storage_bucket",
            match={
                "values.name": "my-bucket",
                "values.location": "US",
            },
            comparison_rule={
                "values.name": "starts_with",
            },
        )

        # Mock resource with name starting with "my-bucket"
        resource = {
            "type": "google_storage_bucket",
            "values": {
                "name": "my-bucket-user1-xyz",
                "location": "US",
            },
        }

        # Mock parser
        class MockParser:
            def get_resource_attribute(self, resource, path):
                parts = path.split(".")
                obj = resource
                for part in parts:
                    obj = obj.get(part, None)
                    if obj is None:
                        return None
                return obj

        parser = MockParser()
        result = _default_validate(inv, resource, parser)

        assert result.passed
        assert len(result.errors) == 0

    def test_validate_storage_bucket_starts_with_failure(self):
        """Test validation fails when starts_with doesn't match."""
        from modules.evaluation.validation.resource_validators import _default_validate

        inv = Invariant(
            resource_type="google_storage_bucket",
            match={
                "values.name": "my-bucket",
            },
            comparison_rule={
                "values.name": "starts_with",
            },
        )

        resource = {
            "type": "google_storage_bucket",
            "values": {
                "name": "other-bucket-xyz",
            },
        }

        class MockParser:
            def get_resource_attribute(self, resource, path):
                parts = path.split(".")
                obj = resource
                for part in parts:
                    obj = obj.get(part, None)
                    if obj is None:
                        return None
                return obj

        parser = MockParser()
        result = _default_validate(inv, resource, parser)

        assert not result.passed
        assert len(result.errors) > 0
        assert "start with" in result.errors[0]

    def test_validate_mixed_comparison_rules(self):
        """Test validating resource with mixed comparison rules."""
        from modules.evaluation.validation.resource_validators import _default_validate

        inv = Invariant(
            resource_type="google_storage_bucket",
            match={
                "values.name": "my-bucket",
                "values.location": "US",
                "values.storage_class": "STANDARD",
            },
            comparison_rule={
                "values.name": "starts_with",
                # values.location and values.storage_class use default exact_match
            },
        )

        resource = {
            "type": "google_storage_bucket",
            "values": {
                "name": "my-bucket-prod-123",
                "location": "US",
                "storage_class": "STANDARD",
            },
        }

        class MockParser:
            def get_resource_attribute(self, resource, path):
                parts = path.split(".")
                obj = resource
                for part in parts:
                    obj = obj.get(part, None)
                    if obj is None:
                        return None
                return obj

        parser = MockParser()
        result = _default_validate(inv, resource, parser)

        assert result.passed
        assert len(result.errors) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
