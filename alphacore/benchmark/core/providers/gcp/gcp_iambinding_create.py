
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
from google.cloud import resourcemanager

def gcp_iambinding_create(task_id: str, params: dict) -> ACTaskSpec:
    resource = params.get("resource", "projects/default-project")
    role = params.get("role", "roles/viewer")
    member = params.get("member", "user:default@example.com")
    verify = VerifyPlan(
        kind="gcp.readback.get",
        steps=[{"op": "projects.getIamPolicy", "resource": resource}],
    )
    def verify_fn(spec, validator_ctx):
        if not resourcemanager:
            return False
        try:
            client = resourcemanager.ProjectsClient()
            policy = client.get_iam_policy(request={"resource": spec.params["resource"]})
            for binding in policy.bindings:
                if binding.role == spec.params["role"] and spec.params["member"] in binding.members:
                    return True
            return False
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="gcp",
        kind="gcp.iambinding.create",
        params={"resource": resource, "role": role, "member": member, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
