from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time

try:
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.resource import ResourceManagementClient
except ImportError:
    DefaultAzureCredential = None
    ResourceManagementClient = None

def azure_resourcegroup_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"ac-rg-{task_id}")
    subscription_id = params.get("subscriptionId", "default-sub")
    location = params.get("location", "eastus")
    verify = VerifyPlan(
        kind="azure.readback.get",
        steps=[{"op": "resourcegroups.get", "name": name}],
    )
    def verify_fn(spec, validator_ctx):
        if not DefaultAzureCredential or not ResourceManagementClient:
            return False
        try:
            credential = DefaultAzureCredential()
            client = ResourceManagementClient(credential, spec.params["subscriptionId"])
            rg = client.resource_groups.get(spec.params["name"])
            return rg.location == spec.params["location"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="azure",
        kind="azure.resourcegroup.create",
        params={"name": name, "subscriptionId": subscription_id, "location": location, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
