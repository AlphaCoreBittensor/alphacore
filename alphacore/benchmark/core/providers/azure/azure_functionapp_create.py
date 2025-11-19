from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time

try:
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.web import WebSiteManagementClient
except ImportError:
    DefaultAzureCredential = None
    WebSiteManagementClient = None

def azure_functionapp_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"acfunc{task_id}")
    location = params.get("location", "eastus")
    kind = params.get("kind", "functionapp")
    server_farm_id = params.get("serverFarmId", "default-plan")
    resource_group = params.get("resourceGroup", "default-rg")
    subscription_id = params.get("subscriptionId", "default-sub")
    verify = VerifyPlan(
        kind="azure.readback.get",
        steps=[{"op": "sites.get", "name": name}],
    )
    def verify_fn(spec, validator_ctx):
        if not DefaultAzureCredential or not WebSiteManagementClient:
            return False
        try:
            credential = DefaultAzureCredential()
            client = WebSiteManagementClient(credential, subscription_id)
            app = client.web_apps.get(resource_group, name)
            return app.location == spec.params["location"] and app.kind == spec.params["kind"] and app.server_farm_id == spec.params["serverFarmId"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="azure",
        kind="azure.functionapp.create",
        params={"name": name, "location": location, "kind": kind, "serverFarmId": server_farm_id, "resourceGroup": resource_group, "subscriptionId": subscription_id, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
