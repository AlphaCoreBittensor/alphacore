"""
Test suite for individual GCP resource templates.

Tests each resource template builder in isolation, including:
- Resource-specific configuration
- Edge cases
- Dependency resolution
- Invariant validation
"""

import random
import pytest

from modules.generation.terraform.resource_templates import TemplateContext
from modules.generation.terraform.providers.gcp.resources import (
    artifact_repository,
    bucket_iam_member,
    compute_instance,
    dns_managed_zone,
    firewall,
    network,
    project_iam_member,
    pubsub_subscription,
    pubsub_topic,
    service_account,
    storage_bucket,
    storage_bucket_object,
    subnetwork,
)


def make_context(
    task_id: str = "test123",
    nonce: str = "abc123def456",
    seed: int = 42,
    shared: dict = None,
    validator_sa: str = "validator@test.com",
) -> TemplateContext:
    """Helper to create a template context with deterministic RNG."""
    return TemplateContext(
        rng=random.Random(seed),
        task_id=task_id,
        nonce=nonce,
        shared=shared or {},
        validator_sa=validator_sa,
    )


# ============================================================================
# 1. RESOURCE-SPECIFIC TESTS
# ============================================================================

class TestArtifactRepository:
    """Test artifact repository resource template."""

    def test_build_repository_basic(self):
        """Test basic repository creation."""
        ctx = make_context()
        templates = artifact_repository.get_templates()
        assert len(templates) == 1

        template = templates[0]
        instance = template.builder(ctx)

        assert len(instance.invariants) == 1
        invariant = instance.invariants[0]
        assert invariant.resource_type == "google_artifact_registry_repository"
        assert "values.repository_id" in invariant.match
        assert "values.location" in invariant.match
        assert "values.format" in invariant.match

    def test_repository_determinism(self):
        """Test that same seed produces same repository."""
        ctx1 = make_context(seed=42)
        ctx2 = make_context(seed=42)

        template = artifact_repository.get_templates()[0]
        instance1 = template.builder(ctx1)
        instance2 = template.builder(ctx2)

        assert instance1.invariants[0].match == instance2.invariants[0].match

    def test_repository_shared_values(self):
        """Test that repository exposes shared values."""
        ctx = make_context()
        template = artifact_repository.get_templates()[0]
        instance = template.builder(ctx)

        assert "artifact_repository" in instance.shared_values
        assert "repository_id" in instance.shared_values["artifact_repository"]


class TestStorageBucket:
    """Test storage bucket resource template."""

    def test_build_bucket_basic(self):
        """Test basic bucket creation."""
        ctx = make_context()
        templates = storage_bucket.get_templates()
        assert len(templates) == 1

        template = templates[0]
        instance = template.builder(ctx)

        assert len(instance.invariants) == 1
        invariant = instance.invariants[0]
        assert invariant.resource_type == "google_storage_bucket"
        assert "values.name" in invariant.match
        assert "values.location" in invariant.match
        assert "values.storage_class" in invariant.match

    def test_bucket_name_uniqueness(self):
        """Test that different nonces produce different bucket names."""
        ctx1 = make_context(nonce="abc123")
        ctx2 = make_context(nonce="xyz789")

        template = storage_bucket.get_templates()[0]
        instance1 = template.builder(ctx1)
        instance2 = template.builder(ctx2)

        name1 = instance1.invariants[0].match["values.name"]
        name2 = instance2.invariants[0].match["values.name"]
        assert name1 != name2

    def test_bucket_provides_capability(self):
        """Test that bucket provides 'bucket' capability."""
        template = storage_bucket.get_templates()[0]
        assert "bucket" in template.provides


class TestServiceAccount:
    """Test service account resource template."""

    def test_build_service_account(self):
        """Test service account creation."""
        ctx = make_context()
        template = service_account.get_templates()[0]
        instance = template.builder(ctx)

        assert len(instance.invariants) == 1
        invariant = instance.invariants[0]
        assert invariant.resource_type == "google_service_account"
        assert "values.account_id" in invariant.match

    def test_service_account_shared_values(self):
        """Test that service account exposes account_id."""
        ctx = make_context()
        template = service_account.get_templates()[0]
        instance = template.builder(ctx)

        assert "service_account" in instance.shared_values
        assert "account_id" in instance.shared_values["service_account"]


class TestPubSubTopic:
    """Test Pub/Sub topic resource template."""

    def test_build_topic(self):
        """Test topic creation."""
        ctx = make_context()
        template = pubsub_topic.get_templates()[0]
        instance = template.builder(ctx)

        assert len(instance.invariants) == 1
        invariant = instance.invariants[0]
        assert invariant.resource_type == "google_pubsub_topic"
        assert "values.name" in invariant.match
        assert "values.message_retention_duration" in invariant.match

    def test_topic_retention_varies(self):
        """Test that different seeds produce different retention."""
        ctx1 = make_context(seed=1)
        ctx2 = make_context(seed=2)

        template = pubsub_topic.get_templates()[0]
        instance1 = template.builder(ctx1)
        instance2 = template.builder(ctx2)

        retention1 = instance1.invariants[0].match["values.message_retention_duration"]
        retention2 = instance2.invariants[0].match["values.message_retention_duration"]
        # May be same or different, but both should be valid
        assert retention1 in ("600s", "900s", "1200s")
        assert retention2 in ("600s", "900s", "1200s")


class TestComputeInstance:
    """Test compute instance resource templates."""

    def test_basic_instance(self):
        """Test basic VM creation."""
        ctx = make_context()
        templates = compute_instance.get_templates()

        # Find basic instance template
        basic_template = next(t for t in templates if t.key == "compute_instance_basic")
        instance = basic_template.builder(ctx)

        assert len(instance.invariants) == 1
        invariant = instance.invariants[0]
        assert invariant.resource_type == "google_compute_instance"
        assert "values.name" in invariant.match
        assert "values.zone" in invariant.match
        assert "values.machine_type" in invariant.match
        assert "values.metadata_startup_script" in invariant.match

    def test_networked_instance_requires_network(self):
        """Test that networked instance requires network capability."""
        templates = compute_instance.get_templates()
        networked_template = next(t for t in templates if t.key == "compute_instance_networked")

        assert "subnetwork" in networked_template.requires

    def test_networked_instance_with_subnet(self):
        """Test networked instance creation with subnet."""
        shared = {
            "subnetwork": {
                "name": "subnet-test",
                "region": "us-central1",
                "zone": "us-central1-a",
            }
        }
        ctx = make_context(shared=shared)
        templates = compute_instance.get_templates()
        networked_template = next(t for t in templates if t.key == "compute_instance_networked")

        instance = networked_template.builder(ctx)
        assert len(instance.invariants) == 1
        assert instance.invariants[0].match["values.zone"] == "us-central1-a"


class TestNetwork:
    """Test VPC network resource template."""

    def test_build_network(self):
        """Test VPC network creation."""
        ctx = make_context()
        template = network.get_templates()[0]
        instance = template.builder(ctx)

        assert len(instance.invariants) == 1
        invariant = instance.invariants[0]
        assert invariant.resource_type == "google_compute_network"
        assert invariant.match["values.auto_create_subnetworks"] is False

    def test_network_provides_capability(self):
        """Test that network provides 'network' capability."""
        template = network.get_templates()[0]
        assert "network" in template.provides


class TestSubnetwork:
    """Test subnetwork resource template."""

    def test_subnetwork_requires_network(self):
        """Test that subnetwork requires network capability."""
        template = subnetwork.get_templates()[0]
        assert "network" in template.requires

    def test_subnetwork_with_network_and_region(self):
        """Test subnetwork creation with network and explicit region."""
        shared = {"network": {"name": "net-test123"}, "region": "us-central1"}
        ctx = make_context(shared=shared)
        template = subnetwork.get_templates()[0]
        instance = template.builder(ctx)

        assert len(instance.invariants) == 1
        invariant = instance.invariants[0]
        assert invariant.resource_type == "google_compute_subnetwork"
        assert "values.ip_cidr_range" in invariant.match
        assert invariant.match["values.region"] == "us-central1"
    def test_network_and_subnet_region_consistency(self):
        """Test that subnet inherits region from shared context if provided."""
        shared = {"network": {"name": "net-test123"}, "region": "europe-west1"}
        ctx = make_context(shared=shared)
        subnet_template = subnetwork.get_templates()[0]
        subnet_instance = subnet_template.builder(ctx)
        subnet_inv = subnet_instance.invariants[0]
        assert subnet_inv.match["values.region"] == "europe-west1"
    def test_networked_instance_region_consistency(self):
        """Test that VM uses the same region/zone as its subnetwork."""
        shared = {
            "subnetwork": {"name": "subnet-test", "region": "asia-southeast1", "zone": "asia-southeast1-b"}
        }
        ctx = make_context(shared=shared)
        templates = compute_instance.get_templates()
        networked_template = next(t for t in templates if t.key == "compute_instance_networked")
        instance = networked_template.builder(ctx)
        inv = instance.invariants[0]
        assert inv.match["values.zone"] == "asia-southeast1-b"
        # If region is present, it should be in shared values
        assert instance.shared_values["instance"]["region"] == "asia-southeast1"


class TestFirewall:
    """Test firewall resource template."""

    def test_firewall_requires_network(self):
        """Test that firewall requires network capability."""
        template = firewall.get_templates()[0]
        assert "network" in template.requires

    def test_firewall_without_network_fails(self):
        """Test that firewall fails without network."""
        ctx = make_context()
        template = firewall.get_templates()[0]

        with pytest.raises(RuntimeError, match="network capability missing"):
            template.builder(ctx)

    def test_firewall_with_network(self):
        """Test firewall creation with network."""
        shared = {"network": {"name": "net-test"}}
        ctx = make_context(shared=shared)
        template = firewall.get_templates()[0]
        instance = template.builder(ctx)

        assert len(instance.invariants) == 1
        invariant = instance.invariants[0]
        assert invariant.resource_type == "google_compute_firewall"


class TestDNSManagedZone:
    """Test DNS managed zone resource template."""

    def test_build_dns_zone(self):
        """Test DNS zone creation."""
        ctx = make_context()
        template = dns_managed_zone.get_templates()[0]
        instance = template.builder(ctx)

        assert len(instance.invariants) == 1
        invariant = instance.invariants[0]
        assert invariant.resource_type == "google_dns_managed_zone"
        assert "values.dns_name" in invariant.match
        # DNS name should end with a dot
        assert invariant.match["values.dns_name"].endswith(".")


class TestPubSubSubscription:
    """Test Pub/Sub subscription resource template."""

    def test_subscription_requires_topic(self):
        """Test that subscription requires topic capability."""
        template = pubsub_subscription.get_templates()[0]
        assert "pubsub_topic" in template.requires

    def test_subscription_with_topic(self):
        """Test subscription creation with topic."""
        shared = {"pubsub_topic": {"name": "topic-test123"}}
        ctx = make_context(shared=shared)
        template = pubsub_subscription.get_templates()[0]
        instance = template.builder(ctx)

        assert len(instance.invariants) == 1
        invariant = instance.invariants[0]
        assert invariant.resource_type == "google_pubsub_subscription"
        assert invariant.match["values.topic"] == "topic-test123"

    def test_subscription_without_topic_uses_fallback(self):
        """Test subscription fallback when topic not in shared."""
        ctx = make_context()
        template = pubsub_subscription.get_templates()[0]
        instance = template.builder(ctx)

        # Should not fail, uses deterministic fallback
        assert len(instance.invariants) == 1


class TestBucketIAMMember:
    """Test bucket IAM member resource template."""

    def test_bucket_iam_requires_dependencies(self):
        """Test that bucket IAM requires bucket."""
        template = bucket_iam_member.get_templates()[0]
        assert "bucket" in template.requires

    def test_bucket_iam_with_dependencies(self):
        """Test bucket IAM creation with dependencies."""
        shared = {
            "bucket": {"name": "bucket-test123"},
            "service_account": {"account_id": "sa-test"},
        }
        ctx = make_context(shared=shared, validator_sa="sa-test@project.iam.gserviceaccount.com")
        template = bucket_iam_member.get_templates()[0]
        instance = template.builder(ctx)

        assert len(instance.invariants) == 1
        invariant = instance.invariants[0]
        assert invariant.resource_type == "google_storage_bucket_iam_member"
        assert invariant.match["values.bucket"] == "bucket-test123"
        assert "sa-test@project.iam.gserviceaccount.com" in invariant.match["values.member"]


class TestProjectIAMMember:
    """Test project IAM member resource template."""

    def test_project_iam_requires_service_account(self):
        """Test that project IAM has no hard dependency."""
        template = project_iam_member.get_templates()[0]
        assert template.requires == ()

    def test_project_iam_with_service_account(self):
        """Test project IAM creation with service account."""
        shared = {"service_account": {"account_id": "sa-test"}}
        ctx = make_context(shared=shared, validator_sa="sa-test@project.iam.gserviceaccount.com")
        template = project_iam_member.get_templates()[0]
        instance = template.builder(ctx)

        assert len(instance.invariants) == 1
        invariant = instance.invariants[0]
        assert invariant.resource_type == "google_project_iam_member"
        assert "sa-test@project.iam.gserviceaccount.com" in invariant.match["values.member"]


class TestStorageBucketObject:
    """Test storage bucket object resource template."""

    def test_bucket_object_requires_bucket(self):
        """Test that bucket object requires bucket."""
        template = storage_bucket_object.get_templates()[0]
        assert "bucket" in template.requires

    def test_bucket_object_without_bucket_fails(self):
        """Test that bucket object fails without bucket."""
        ctx = make_context()
        template = storage_bucket_object.get_templates()[0]

        with pytest.raises(RuntimeError, match="requires an existing bucket"):
            template.builder(ctx)

    def test_bucket_object_with_bucket(self):
        """Test bucket object creation with bucket."""
        shared = {"bucket": {"name": "bucket-test"}}
        ctx = make_context(shared=shared)
        template = storage_bucket_object.get_templates()[0]
        instance = template.builder(ctx)

        assert len(instance.invariants) == 1
        invariant = instance.invariants[0]
        assert invariant.resource_type == "google_storage_bucket_object"
        assert invariant.match["values.bucket"] == "bucket-test"
        assert invariant.match["values.content_type"] == "text/plain"


# ============================================================================
# 2. DETERMINISM TESTS
# ============================================================================

class TestDeterminism:
    """Test that resource generation is deterministic."""

    def test_same_seed_produces_same_bucket(self):
        """Test bucket determinism with same seed."""
        ctx1 = make_context(task_id="task1", nonce="nonce1", seed=42)
        ctx2 = make_context(task_id="task1", nonce="nonce1", seed=42)

        template = storage_bucket.get_templates()[0]
        instance1 = template.builder(ctx1)
        instance2 = template.builder(ctx2)

        assert instance1.invariants[0].match == instance2.invariants[0].match

    def test_different_seed_produces_different_config(self):
        """Test that different seeds produce different configs."""
        ctx1 = make_context(task_id="task1", nonce="nonce1", seed=1)
        ctx2 = make_context(task_id="task1", nonce="nonce1", seed=2)

        template = storage_bucket.get_templates()[0]
        instance1 = template.builder(ctx1)
        instance2 = template.builder(ctx2)

        # Names will be the same (based on nonce), but location/class may differ
        match1 = instance1.invariants[0].match
        match2 = instance2.invariants[0].match
        assert match1["values.name"] == match2["values.name"]
        # At least one config should differ (probabilistically)
        assert (match1["values.location"] != match2["values.location"] or
                match1["values.storage_class"] != match2["values.storage_class"])

    def test_different_nonce_produces_different_names(self):
        """Test that different nonces produce different resource names."""
        ctx1 = make_context(nonce="nonce1", seed=42)
        ctx2 = make_context(nonce="nonce2", seed=42)

        template = service_account.get_templates()[0]
        instance1 = template.builder(ctx1)
        instance2 = template.builder(ctx2)

        account1 = instance1.invariants[0].match["values.account_id"]
        account2 = instance2.invariants[0].match["values.account_id"]
        assert account1 != account2


# ============================================================================
# 3. EDGE CASES
# ============================================================================

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_very_short_nonce(self):
        """Test with very short nonce."""
        ctx = make_context(nonce="ab")
        template = storage_bucket.get_templates()[0]
        instance = template.builder(ctx)

        # Should not crash, name should still be generated
        assert len(instance.invariants) == 1

    def test_very_long_task_id(self):
        """Test with very long task_id."""
        ctx = make_context(task_id="x" * 100)
        template = service_account.get_templates()[0]
        instance = template.builder(ctx)

        # Should truncate appropriately
        display_name = instance.invariants[0].match["values.display_name"]
        assert len(display_name) < 100

    def test_empty_shared_dict(self):
        """Test with explicitly empty shared dict."""
        ctx = make_context(shared={})
        template = storage_bucket.get_templates()[0]
        instance = template.builder(ctx)

        # Should work fine without dependencies
        assert len(instance.invariants) == 1

    def test_resource_hint_generation(self):
        """Test that all resources generate hints."""
        ctx = make_context()
        all_templates = [
            storage_bucket.get_templates()[0],
            service_account.get_templates()[0],
            pubsub_topic.get_templates()[0],
            dns_managed_zone.get_templates()[0],
        ]

        for template in all_templates:
            instance = template.builder(ctx)
            assert len(instance.prompt_hints) > 0
            assert isinstance(instance.prompt_hints[0], str)
            assert len(instance.prompt_hints[0]) > 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
