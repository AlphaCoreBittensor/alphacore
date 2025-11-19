from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time

try:
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.storage import StorageManagementClient
except ImportError:
    DefaultAzureCredential = None
    StorageManagementClient = None

def azure_queue_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"acqueue{task_id}")
    storage_account = params.get("storageAccount", "defaultstorage")
    resource_group = params.get("resourceGroup", "default-rg")
    subscription_id = params.get("subscriptionId", "default-sub")
    verify = VerifyPlan(
        kind="azure.readback.get",
        steps=[{"op": "queueServices.get", "name": name, "storageAccount": storage_account}],
    )
    def verify_fn(spec, validator_ctx):
        if not DefaultAzureCredential or not StorageManagementClient:
            return False
        try:
            credential = DefaultAzureCredential()
            client = StorageManagementClient(credential, subscription_id)
            queue = client.queue.get(resource_group, storage_account, name)
            return queue.name == spec.params["name"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="azure",
        kind="azure.queue.create",
        params={"name": name, "storageAccount": storage_account, "resourceGroup": resource_group, "subscriptionId": subscription_id, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
