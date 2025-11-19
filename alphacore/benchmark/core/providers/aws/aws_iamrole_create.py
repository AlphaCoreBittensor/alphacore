
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
import boto3

def aws_iamrole_create(task_id: str, params: dict) -> ACTaskSpec:
    role_name = params.get("name", f"ac-role-{task_id}")
    path = params.get("path", "/")
    region = params.get("region", "us-east-1")
    verify = VerifyPlan(
        kind="aws.readback.describe",
        steps=[{"op": "iam.get_role", "name": role_name}],
    )
    def verify_fn(spec: ACTaskSpec, validator_ctx: dict) -> bool:
        session = boto3.Session(
            aws_access_key_id=validator_ctx.get("aws_access_key_id"),
            aws_secret_access_key=validator_ctx.get("aws_secret_access_key"),
            region_name=spec.params.get("region", "us-east-1")
        )
        iam = session.client("iam")
        try:
            resp = iam.get_role(RoleName=spec.params["name"])
            return 'Role' in resp and resp['Role']['Path'] == spec.params['path']
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="aws",
        kind="aws.iamrole.create",
        params={"name": role_name, "path": path, "region": region, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
