
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
from google.cloud import run_v2

def gcp_cloudrunservice_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"ac-run-{task_id}")
    location = params.get("location", "us-central1")
    project = params.get("projectId", "default-project")
    verify = VerifyPlan(
        kind="gcp.readback.get",
        steps=[{"op": "run.projects.locations.services.get", "name": name}],
    )
    def verify_fn(spec, validator_ctx):
        if not run_v2:
            return False
        try:
            client = run_v2.ServicesClient()
            service = client.get_service(name=f"projects/{spec.params['projectId']}/locations/{spec.params['location']}/services/{spec.params['name']}")
            return service.name.split("/")[-1] == spec.params["name"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="gcp",
        kind="gcp.cloudrunservice.create",
        params={"name": name, "location": location, "projectId": project, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
