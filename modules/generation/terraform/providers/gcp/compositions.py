from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List, Sequence, Tuple


@dataclass(frozen=True)
class CompositionFamily:
    """
    Simple struct describing which resource templates belong to a task family.

    - name: descriptive label for telemetry/debugging.
    - mandatory: templates that must always be present.
    - optional: templates that are sampled per-task.
    - min_optional/max_optional: bounds for how many optional templates to attach.
    """

    name: str
    mandatory: Tuple[str, ...]
    optional: Tuple[str, ...] = ()
    min_optional: int = 0
    max_optional: int | None = None

    def pick_templates(self, rng: random.Random) -> List[str]:
        selection: List[str] = list(self.mandatory)
        if not self.optional:
            return selection
        ceiling = len(self.optional) if self.max_optional is None else min(
            self.max_optional, len(self.optional)
        )
        floor = min(self.min_optional, ceiling)
        count = rng.randint(floor, ceiling) if ceiling > 0 else 0
        if count > 0:
            extras = rng.sample(self.optional, count)
            selection.extend(extras)
        return selection


SINGLE_RESOURCE_FAMILIES: Sequence[CompositionFamily] = (
    CompositionFamily("single_vm", ("compute_instance_basic",)),
    CompositionFamily("single_bucket", ("storage_bucket",)),
    CompositionFamily("single_artifact_repo", ("artifact_repository",)),
    CompositionFamily("single_pubsub_topic", ("pubsub_topic",)),
    CompositionFamily("single_dns_zone", ("dns_managed_zone",)),
    CompositionFamily("single_secret", ("secret_manager_secret",)),
)

COMPOSITE_FAMILIES: Sequence[CompositionFamily] = (
    CompositionFamily(
        name="network_stack",
        mandatory=("vpc_network", "subnetwork", "firewall_rule", "compute_instance_networked"),
    ),
    CompositionFamily(
        name="network_without_instance",
        mandatory=("vpc_network", "subnetwork", "firewall_rule"),
        optional=("compute_instance_basic",),
        min_optional=0,
        max_optional=1,
    ),
    CompositionFamily(
        name="storage_delivery",
        mandatory=("storage_bucket",),
        optional=("artifact_repository", "pubsub_topic"),
        min_optional=1,
        max_optional=2,
    ),
    CompositionFamily(
        name="artifact_delivery",
        mandatory=("artifact_repository",),
        optional=("storage_bucket", "pubsub_topic", "storage_bucket_object"),
        min_optional=1,
        max_optional=3,
    ),
    CompositionFamily(
        name="bucket_with_object",
        mandatory=("storage_bucket", "storage_bucket_object"),
        optional=("pubsub_topic",),
        min_optional=0,
        max_optional=1,
    ),
    CompositionFamily(
        name="network_plus_storage",
        mandatory=(
            "vpc_network",
            "subnetwork",
            "firewall_rule",
            "compute_instance_networked",
            "storage_bucket",
        ),
        optional=("storage_bucket_object", "artifact_repository"),
        min_optional=1,
        max_optional=2,
    ),
    CompositionFamily(
        name="network_plus_artifacts",
        mandatory=(
            "vpc_network",
            "subnetwork",
            "firewall_rule",
            "compute_instance_networked",
            "artifact_repository",
        ),
        optional=("storage_bucket", "storage_bucket_object"),
        min_optional=1,
        max_optional=1,
    ),
    CompositionFamily(
        name="network_plus_pubsub",
        mandatory=(
            "vpc_network",
            "subnetwork",
            "firewall_rule",
            "compute_instance_networked",
            "pubsub_topic",
        ),
        optional=("storage_bucket", "storage_bucket_object"),
        min_optional=0,
        max_optional=2,
    ),
    CompositionFamily(
        name="storage_with_pubsub",
        mandatory=("storage_bucket", "pubsub_topic"),
        optional=("artifact_repository", "storage_bucket_object"),
        min_optional=1,
        max_optional=2,
    ),
    CompositionFamily(
        name="service_account_delivery",
        mandatory=("service_account",),
        optional=("storage_bucket", "artifact_repository", "pubsub_topic", "storage_bucket_object"),
        min_optional=2,
        max_optional=4,
    ),
    CompositionFamily(
        name="dns_frontend",
        mandatory=("dns_managed_zone", "storage_bucket"),
        optional=("storage_bucket_object", "pubsub_topic"),
        min_optional=1,
        max_optional=2,
    ),
    CompositionFamily(
        name="dns_network_bridge",
        mandatory=("dns_managed_zone", "vpc_network", "subnetwork", "compute_instance_networked"),
        optional=("firewall_rule", "storage_bucket"),
        min_optional=1,
        max_optional=2,
    ),
    CompositionFamily(
        name="network_plus_service_account",
        mandatory=("vpc_network", "subnetwork", "firewall_rule", "compute_instance_networked", "service_account"),
        optional=("storage_bucket", "artifact_repository", "storage_bucket_object"),
        min_optional=1,
        max_optional=3,
    ),
    CompositionFamily(
        name="bucket_object_with_iam",
        mandatory=("storage_bucket", "storage_bucket_object", "service_account", "bucket_iam_member"),
        optional=("pubsub_topic",),
        min_optional=0,
        max_optional=1,
    ),
    CompositionFamily(
        name="project_with_iam",
        mandatory=("service_account", "project_iam_member"),
        optional=("storage_bucket", "compute_instance_basic"),
        min_optional=1,
        max_optional=2,
    ),
    CompositionFamily(
        name="topic_with_subscription",
        mandatory=("pubsub_topic", "pubsub_subscription"),
        optional=("storage_bucket", "service_account"),
        min_optional=0,
        max_optional=2,
    ),
)
