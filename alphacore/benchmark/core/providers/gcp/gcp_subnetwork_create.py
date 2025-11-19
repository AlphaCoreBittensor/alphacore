
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
from google.cloud import compute_v1

def gcp_subnetwork_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"ac-subnet-{task_id}")
    network = params.get("network", "default-vpc")
    region = params.get("region", "us-central1")
    ip_cidr_range = params.get("ipCidrRange", "10.0.0.0/24")
    project = params.get("projectId", "default-project")
    verify = VerifyPlan(
        kind="gcp.readback.get",
        steps=[{"op": "compute.subnetworks.get", "name": name}],
    )
    def verify_fn(spec, validator_ctx):
        if not compute_v1:
            return False
        try:
            client = compute_v1.SubnetworksClient()
            subnet = client.get(project=spec.params["projectId"], region=spec.params["region"], subnetwork=spec.params["name"])
            return subnet.ip_cidr_range == spec.params["ipCidrRange"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="gcp",
        kind="gcp.subnetwork.create",
        params={"name": name, "network": network, "region": region, "ipCidrRange": ip_cidr_range, "projectId": project, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
