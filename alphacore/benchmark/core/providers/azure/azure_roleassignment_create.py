from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time

try:
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.authorization import AuthorizationManagementClient
except ImportError:
    DefaultAzureCredential = None
    AuthorizationManagementClient = None

def azure_roleassignment_create(task_id: str, params: dict) -> ACTaskSpec:
    principal_id = params.get("principalId", "default-principal")
    role_definition_id = params.get("roleDefinitionId", "default-roledef")
    scope = params.get("scope", "default-scope")
    subscription_id = params.get("subscriptionId", "default-sub")
    verify = VerifyPlan(
        kind="azure.readback.get",
        steps=[{"op": "roleAssignments.get", "principalId": principal_id, "roleDefinitionId": role_definition_id, "scope": scope}],
    )
    def verify_fn(spec, validator_ctx):
        if not DefaultAzureCredential or not AuthorizationManagementClient:
            return False
        try:
            credential = DefaultAzureCredential()
            client = AuthorizationManagementClient(credential, subscription_id)
            assignments = client.role_assignments.list_for_scope(scope)
            for assignment in assignments:
                if assignment.principal_id == spec.params["principalId"] and assignment.role_definition_id == spec.params["roleDefinitionId"]:
                    return True
            return False
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="azure",
        kind="azure.roleassignment.create",
        params={"principalId": principal_id, "roleDefinitionId": role_definition_id, "scope": scope, "subscriptionId": subscription_id, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
