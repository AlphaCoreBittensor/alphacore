
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
from google.cloud import storage

def gcp_storagebucket_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"ac-bucket-{task_id}")
    location = params.get("location", "us-central1")
    project = params.get("projectId", "default-project")
    verify = VerifyPlan(
        kind="gcp.readback.get",
        steps=[{"op": "storage.buckets.get", "name": name}],
    )
    def verify_fn(spec, validator_ctx):
        if not storage:
            return False
        try:
            client = storage.Client(project=spec.params["projectId"])
            bucket = client.get_bucket(spec.params["name"])
            return bucket.location == spec.params["location"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="gcp",
        kind="gcp.storagebucket.create",
        params={"name": name, "location": location, "projectId": project, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
