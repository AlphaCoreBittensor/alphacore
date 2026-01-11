from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)
from modules.generation.terraform.providers.gcp import helpers


def _build_subscription(ctx: TemplateContext) -> ResourceInstance:
    shared_topic = ctx.shared.get("pubsub_topic")
    # Be resilient to ordering by deriving the deterministic topic id when shared is not yet populated.
    topic_name = (shared_topic or {}).get("name") or helpers.pubsub_topic_id(ctx.nonce[:8])
    name = helpers.pubsub_subscription_id(ctx.nonce[:8])
    ack_deadline = helpers.pubsub_ack_deadline(ctx.rng)
    ttl = helpers.pubsub_expiration_ttl(ctx.rng)
    retain_acked = ctx.rng.choice([True, False])

    invariant = Invariant(
        resource_type="google_pubsub_subscription",
        match={
            "values.name": name,
            "values.topic": topic_name,
            "values.ack_deadline_seconds": ack_deadline,
            "values.retain_acked_messages": retain_acked,
            "values.expiration_policy.0.ttl": ttl,
        },
    )
    retention_hint = "retaining" if retain_acked else "discarding"
    hint = f"Create a Pub/Sub subscription {name} bound to topic {topic_name} with {ack_deadline}s ack deadline, {ttl} expiration TTL, and {retention_hint} acked messages."
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"subscription": {"name": name}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="pubsub_subscription",
            kind="pubsub subscription",
            provides=("pubsub_subscription",),
            requires=("pubsub_topic",),
            builder=_build_subscription,
            base_hints=("Keep the subscription minimal with short ack deadline.",),
            weight=0.9,
            naming_rules=("starts_with", "ends_with", "exact_match"),
        )
    ]
