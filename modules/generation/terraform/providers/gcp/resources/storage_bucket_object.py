from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _build_bucket_object(ctx: TemplateContext) -> ResourceInstance:
    bucket = ctx.shared.get("bucket")
    if not bucket:
        raise RuntimeError("storage_bucket_object template requires an existing bucket.")
    object_name = helpers.bucket_object_name(ctx.rng)
    content_type = "text/plain"
    # Deterministic, task-specific payload so tasks don't all look identical.
    content = helpers.random_text(ctx.rng, min_len=12, max_len=20)
    invariant = Invariant(
        resource_type="google_storage_bucket_object",
        match={
            "values.name": object_name,
            "values.bucket": bucket["name"],
            "values.content_type": content_type,
            "values.content": content,
        },
    )
    hint = (
        f"Upload a plaintext object {object_name} into bucket {bucket['name']} "
        f"with content '{content}' (content-type {content_type})."
    )
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"bucket_object": {"name": object_name}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="storage_bucket_object",
            kind="bucket object",
            provides=("bucket_object",),
            requires=("bucket",),
            builder=_build_bucket_object,
            base_hints=("Deliver at least one concrete file inside the bucket.",),
            weight=0.9,
        )
    ]
