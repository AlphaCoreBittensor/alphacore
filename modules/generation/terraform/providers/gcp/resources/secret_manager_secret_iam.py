from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _bind_secret_accessor(ctx: TemplateContext) -> ResourceInstance:
    secret = ctx.shared.get("secret_manager_secret")

    secret_id = (secret or {}).get("secret_id") or helpers.secret_id(ctx.nonce[:8])

    role = helpers.secret_iam_role(ctx.rng)
    principal = (ctx.validator_sa or "").strip()
    if principal and "@" in principal:
        prefix = "serviceAccount" if principal.endswith("gserviceaccount.com") else "user"
        member = f"{prefix}:{principal}"
    else:
        member = f"serviceAccount:{helpers.service_account_id(ctx.nonce[:8])}"

    invariant = Invariant(
        resource_type="google_secret_manager_secret_iam_member",
        match={
            "values.secret_id": secret_id,
            "values.role": role,
            "values.member": member,
        },
    )
    hint = f"Grant {role} on secret {secret_id} to {member}."
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"secret_manager_secret_iam": {"role": role, "member": member}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="secret_manager_secret_iam",
            kind="secret manager secret iam member",
            provides=("secret_manager_secret_iam",),
            requires=("secret_manager_secret",),
            builder=_bind_secret_accessor,
            base_hints=("Grant minimal secret access permissions.",),
            weight=0.85,
        )
    ]
