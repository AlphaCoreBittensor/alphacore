from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _build_topic(ctx: TemplateContext) -> ResourceInstance:
    suffix = ctx.nonce[:8]
    topic = helpers.pubsub_topic_id(suffix)
    retention = helpers.pubsub_retention_window(ctx.rng)
    invariant = Invariant(
        resource_type="google_pubsub_topic",
        match={
            "values.name": topic,
            "values.message_retention_duration": retention,
        },
    )
    hint = f"Ensure Pub/Sub topic {topic} retains messages for {retention}."
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"pubsub_topic": {"name": topic}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="pubsub_topic",
            kind="pubsub topic",
            provides=("pubsub_topic",),
            builder=_build_topic,
            base_hints=("Keep the topic isolated with explicit retention.",),
            weight=0.9,
            naming_rules=("starts_with", "ends_with", "exact_match"),
        )
    ]
