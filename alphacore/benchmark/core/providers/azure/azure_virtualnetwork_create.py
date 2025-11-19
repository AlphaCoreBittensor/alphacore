from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time

try:
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.network import NetworkManagementClient
except ImportError:
    DefaultAzureCredential = None
    NetworkManagementClient = None

def azure_virtualnetwork_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"acvnet{task_id}")
    location = params.get("location", "eastus")
    subscription_id = params.get("subscriptionId", "default-sub")
    resource_group = params.get("resourceGroup", "default-rg")
    verify = VerifyPlan(
        kind="azure.readback.get",
        steps=[{"op": "virtualNetworks.get", "name": name}],
    )
    def verify_fn(spec, validator_ctx):
        if not DefaultAzureCredential or not NetworkManagementClient:
            return False
        try:
            credential = DefaultAzureCredential()
            client = NetworkManagementClient(credential, subscription_id)
            vnet = client.virtual_networks.get(resource_group, name)
            return vnet.location == spec.params["location"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="azure",
        kind="azure.virtualnetwork.create",
        params={"name": name, "location": location, "subscriptionId": subscription_id, "resourceGroup": resource_group, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
