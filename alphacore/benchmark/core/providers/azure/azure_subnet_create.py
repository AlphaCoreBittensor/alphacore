from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time

try:
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.network import NetworkManagementClient
except ImportError:
    DefaultAzureCredential = None
    NetworkManagementClient = None

def azure_subnet_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"acsubnet{task_id}")
    vnet = params.get("vnet", "defaultvnet")
    address_prefix = params.get("addressPrefix", "10.0.0.0/24")
    resource_group = params.get("resourceGroup", "default-rg")
    subscription_id = params.get("subscriptionId", "default-sub")
    verify = VerifyPlan(
        kind="azure.readback.get",
        steps=[{"op": "subnets.get", "name": name, "vnet": vnet}],
    )
    def verify_fn(spec, validator_ctx):
        if not DefaultAzureCredential or not NetworkManagementClient:
            return False
        try:
            credential = DefaultAzureCredential()
            client = NetworkManagementClient(credential, subscription_id)
            subnet = client.subnets.get(resource_group, vnet, name)
            return subnet.address_prefix == spec.params["addressPrefix"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="azure",
        kind="azure.subnet.create",
        params={"name": name, "vnet": vnet, "addressPrefix": address_prefix, "resourceGroup": resource_group, "subscriptionId": subscription_id, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
