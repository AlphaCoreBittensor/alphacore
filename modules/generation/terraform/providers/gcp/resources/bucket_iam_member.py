from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _bind_bucket_viewer(ctx: TemplateContext) -> ResourceInstance:
    bucket = ctx.shared.get("bucket")
    # Derive deterministic fallbacks if upstream values not yet populated.
    bucket_name = (bucket or {}).get("name") or helpers.bucket_name(ctx.nonce[:10])

    role = helpers.bucket_iam_role(ctx.rng)
    principal = (ctx.validator_sa or "").strip()
    if principal and "@" in principal:
        prefix = "serviceAccount" if principal.endswith("gserviceaccount.com") else "user"
        member = f"{prefix}:{principal}"
    else:
        member = f"serviceAccount:{helpers.service_account_id(ctx.nonce[:8])}"

    invariant = Invariant(
        resource_type="google_storage_bucket_iam_member",
        match={
            "values.bucket": bucket_name,
            "values.role": role,
            "values.member": member,
        },
    )
    hint = f"Grant {role} on bucket {bucket_name} to {member}."
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"bucket_iam_member": {"role": role, "member": member}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="bucket_iam_member",
            kind="bucket iam member",
            provides=("bucket_iam_member",),
            requires=("bucket",),
            builder=_bind_bucket_viewer,
            base_hints=("Prefer bucket-level viewer over broad project bindings.",),
            weight=0.9,
        )
    ]
