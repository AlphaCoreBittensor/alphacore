from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time

try:
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.storage import StorageManagementClient
except ImportError:
    DefaultAzureCredential = None
    StorageManagementClient = None

def azure_storageaccount_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"acstorage{task_id}")
    location = params.get("location", "eastus")
    kind = params.get("kind", "StorageV2")
    sku_tier = params.get("sku_tier", "Standard")
    subscription_id = params.get("subscriptionId", "default-sub")
    verify = VerifyPlan(
        kind="azure.readback.get",
        steps=[{"op": "storageAccounts.get", "name": name}],
    )
    def verify_fn(spec, validator_ctx):
        if not DefaultAzureCredential or not StorageManagementClient:
            return False
        try:
            credential = DefaultAzureCredential()
            client = StorageManagementClient(credential, subscription_id)
            sa = client.storage_accounts.get_properties(resource_group_name=spec.params.get("resourceGroup", "default-rg"), account_name=spec.params["name"])
            return sa.location == spec.params["location"] and sa.kind == spec.params["kind"] and sa.sku.tier.name == spec.params["sku_tier"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="azure",
        kind="azure.storageaccount.create",
        params={"name": name, "location": location, "kind": kind, "sku_tier": sku_tier, "subscriptionId": subscription_id, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
