
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
from google.cloud import compute_v1

def gcp_firewallrule_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"ac-fw-{task_id}")
    network = params.get("network", "default-vpc")
    direction = params.get("direction", "INGRESS")
    project = params.get("projectId", "default-project")
    verify = VerifyPlan(
        kind="gcp.readback.get",
        steps=[{"op": "compute.firewalls.get", "name": name}],
    )
    def verify_fn(spec, validator_ctx):
        if not compute_v1:
            return False
        try:
            client = compute_v1.FirewallsClient()
            fw = client.get(project=spec.params["projectId"], firewall=spec.params["name"])
            return fw.direction == spec.params["direction"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="gcp",
        kind="gcp.firewallrule.create",
        params={"name": name, "network": network, "direction": direction, "projectId": project, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
