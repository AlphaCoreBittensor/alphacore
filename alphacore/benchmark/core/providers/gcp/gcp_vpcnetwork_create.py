
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
from google.cloud import compute_v1

def gcp_vpcnetwork_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"ac-vpc-{task_id}")
    project = params.get("projectId", "default-project")
    routing_mode = params.get("routingMode", "REGIONAL")
    verify = VerifyPlan(
        kind="gcp.readback.get",
        steps=[{"op": "compute.networks.get", "name": name}],
    )
    def verify_fn(spec, validator_ctx):
        if not compute_v1:
            return False
        try:
            client = compute_v1.NetworksClient()
            network = client.get(project=spec.params["projectId"], network=spec.params["name"])
            return network.routing_config.routing_mode.name == spec.params["routingMode"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="gcp",
        kind="gcp.vpcnetwork.create",
        params={"name": name, "projectId": project, "routingMode": routing_mode, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
