from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _bind_sa_iam_member(ctx: TemplateContext) -> ResourceInstance:
    sa = ctx.shared.get("service_account")
    account_id = (sa or {}).get("account_id") or helpers.service_account_id(ctx.nonce[:8])

    role = helpers.service_account_iam_role(ctx.rng)
    principal = (ctx.validator_sa or "").strip()
    if principal and "@" in principal:
        prefix = "serviceAccount" if principal.endswith("gserviceaccount.com") else "user"
        member = f"{prefix}:{principal}"
    else:
        member = f"serviceAccount:{helpers.service_account_id(ctx.nonce[:8])}"

    invariant = Invariant(
        resource_type="google_service_account_iam_member",
        match={
            "values.service_account_id": account_id,
            "values.role": role,
            "values.member": member,
        },
    )
    hint = f"Grant {role} on service account {account_id} to {member}."
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"service_account_iam_member": {"role": role, "member": member}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="service_account_iam_member",
            kind="service account iam member",
            provides=("service_account_iam_member",),
            requires=("service_account",),
            builder=_bind_sa_iam_member,
            base_hints=("Grant IAM role on service account resource.",),
            weight=0.8,
        )
    ]
