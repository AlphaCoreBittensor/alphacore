
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
from google.cloud import iam

def gcp_serviceaccount_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"ac-sa-{task_id}@default-project.iam.gserviceaccount.com")
    project = params.get("projectId", "default-project")
    verify = VerifyPlan(
        kind="gcp.readback.get",
        steps=[{"op": "iam.serviceAccounts.get", "name": name}],
    )
    def verify_fn(spec, validator_ctx):
        if not iam:
            return False
        try:
            client = iam.IAMClient()
            sa = client.get_service_account(name=f"projects/{spec.params['projectId']}/serviceAccounts/{spec.params['name']}")
            return sa.email == spec.params["name"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="gcp",
        kind="gcp.serviceaccount.create",
        params={"name": name, "projectId": project, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
