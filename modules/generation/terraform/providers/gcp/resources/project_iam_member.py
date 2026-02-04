from __future__ import annotations

from modules.models import Invariant
from modules.generation.terraform.providers.gcp import helpers
from modules.generation.terraform.resource_templates import (
    ResourceInstance,
    ResourceTemplate,
    TemplateContext,
)


def _bind_project_viewer(ctx: TemplateContext) -> ResourceInstance:
    role = helpers.project_iam_role(ctx.rng)
    principal = (ctx.validator_sa or "").strip()
    if principal and "@" in principal:
        prefix = "serviceAccount" if principal.endswith("gserviceaccount.com") else "user"
        member = f"{prefix}:{principal}"
    else:
        member = f"serviceAccount:{helpers.service_account_id(ctx.nonce[:8])}"

    invariant = Invariant(
        resource_type="google_project_iam_member",
        match={
            "values.role": role,
            "values.member": member,
        },
    )
    hint = f"Bind {member} to project-wide {role}."
    return ResourceInstance(
        invariants=[invariant],
        prompt_hints=[hint],
        shared_values={"project_iam_member": {"role": role, "member": member}},
    )


def get_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            key="project_iam_member",
            kind="project iam member",
            provides=("project_iam_member",),
            requires=(),
            builder=_bind_project_viewer,
            base_hints=("Keep project role minimal with viewer access.",),
            weight=0.8,
        )
    ]
