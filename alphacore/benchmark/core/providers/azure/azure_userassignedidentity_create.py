from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time

try:
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.msi import ManagedServiceIdentityClient
except ImportError:
    DefaultAzureCredential = None
    ManagedServiceIdentityClient = None

def azure_userassignedidentity_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"acid{task_id}")
    principal_id = params.get("principalId", "default-principal")
    client_id = params.get("clientId", "default-client")
    resource_group = params.get("resourceGroup", "default-rg")
    subscription_id = params.get("subscriptionId", "default-sub")
    verify = VerifyPlan(
        kind="azure.readback.get",
        steps=[{"op": "userAssignedIdentities.get", "name": name}],
    )
    def verify_fn(spec, validator_ctx):
        if not DefaultAzureCredential or not ManagedServiceIdentityClient:
            return False
        try:
            credential = DefaultAzureCredential()
            client = ManagedServiceIdentityClient(credential, subscription_id)
            identity = client.user_assigned_identities.get(resource_group, name)
            return identity.principal_id == spec.params["principalId"] and identity.client_id == spec.params["clientId"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="azure",
        kind="azure.userassignedidentity.create",
        params={"name": name, "principalId": principal_id, "clientId": client_id, "resourceGroup": resource_group, "subscriptionId": subscription_id, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
