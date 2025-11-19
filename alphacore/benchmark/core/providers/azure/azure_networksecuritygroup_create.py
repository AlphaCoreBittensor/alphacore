from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time

try:
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.network import NetworkManagementClient
except ImportError:
    DefaultAzureCredential = None
    NetworkManagementClient = None

def azure_networksecuritygroup_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"acnsg{task_id}")
    location = params.get("location", "eastus")
    resource_group = params.get("resourceGroup", "default-rg")
    subscription_id = params.get("subscriptionId", "default-sub")
    verify = VerifyPlan(
        kind="azure.readback.get",
        steps=[{"op": "networkSecurityGroups.get", "name": name}],
    )
    def verify_fn(spec, validator_ctx):
        if not DefaultAzureCredential or not NetworkManagementClient:
            return False
        try:
            credential = DefaultAzureCredential()
            client = NetworkManagementClient(credential, subscription_id)
            nsg = client.network_security_groups.get(resource_group, name)
            return nsg.location == spec.params["location"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="azure",
        kind="azure.networksecuritygroup.create",
        params={"name": name, "location": location, "resourceGroup": resource_group, "subscriptionId": subscription_id, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
