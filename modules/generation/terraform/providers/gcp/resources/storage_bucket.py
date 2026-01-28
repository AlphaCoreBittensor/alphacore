from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _build_bucket(ctx: TemplateContext) -> ResourceInstance:
    suffix = ctx.nonce[:10]
    bucket = helpers.bucket_name(suffix)
    location = helpers.bucket_location(ctx.rng)
    storage_class = helpers.bucket_storage_class(ctx.rng)

    invariant = Invariant(
        resource_type="google_storage_bucket",
        match={
            "values.name": bucket,
            "values.location": location,
            "values.storage_class": storage_class,
        },
    )
    hint = f"Provision a Cloud Storage bucket named {bucket} in {location} with {storage_class} storage class."
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"bucket": {"name": bucket}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="storage_bucket",
            kind="storage bucket",
            provides=("bucket",),
            builder=_build_bucket,
            base_hints=("Keep bucket configs minimal yet explicit.",),
            weight=1.1,
            naming_rules=("starts_with", "ends_with"),  # Buckets are globally unique
        )
    ]
