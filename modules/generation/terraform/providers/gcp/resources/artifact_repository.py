from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _build_repository(ctx: TemplateContext) -> ResourceInstance:
    suffix = ctx.nonce[:8]
    repository_id = helpers.artifact_repository_id(suffix)
    location = helpers.artifact_location(ctx.rng)
    fmt = helpers.artifact_format(ctx.rng)
    invariant = Invariant(
        resource_type="google_artifact_registry_repository",
        match={
            "values.repository_id": repository_id,
            "values.location": location,
            "values.format": fmt,
        },
    )
    hint = f"Create Artifact Registry repository {repository_id} in {location} with {fmt} packages."
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"artifact_repository": {"repository_id": repository_id}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="artifact_repository",
            kind="artifact registry repo",
            provides=("artifact_repository",),
            builder=_build_repository,
            base_hints=("Keep the repository minimal; ensure the package format matches the request.",),
            naming_rules=("starts_with", "ends_with"),  # Repository IDs are project-scoped but often globally referenced
        )
    ]
