from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _build_secret(ctx: TemplateContext) -> ResourceInstance:
    secret_id = helpers.secret_id(ctx.nonce[:8])
    secret_data = helpers.secret_payload(ctx.nonce)

    # Two invariants: one for secret, one for secret version
    secret_invariant = Invariant(
        resource_type="google_secret_manager_secret",
        match={
            "values.secret_id": secret_id,
        },
    )

    version_invariant = Invariant(
        resource_type="google_secret_manager_secret_version",
        match={
            "values.secret": secret_id,
            "values.secret_data": secret_data,
        },
    )

    hint = f"Create a Secret Manager secret {secret_id} with a secret version containing the payload."
    return ResourceInstance(
        invariants=[secret_invariant, version_invariant],
        prompt_hints=[hint],
        shared_values={"secret_manager_secret": {"secret_id": secret_id}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="secret_manager_secret",
            kind="secret manager secret",
            provides=("secret_manager_secret",),
            requires=(),
            builder=_build_secret,
            base_hints=("Create secret with at least one version.",),
            weight=0.9,
            naming_rules=("starts_with", "ends_with", "exact_match"),
        )
    ]
