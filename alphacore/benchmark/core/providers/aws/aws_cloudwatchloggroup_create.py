from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
import boto3

def aws_cloudwatchloggroup_create(task_id: str, params: dict) -> ACTaskSpec:
    log_group_name = params.get("name", f"ac-loggroup-{task_id}")
    region = params.get("region", "us-east-1")
    verify = VerifyPlan(
        kind="aws.readback.describe",
        steps=[{"op": "logs.describe_log_groups", "name": log_group_name}],
    )
    def verify_fn(spec: ACTaskSpec, validator_ctx: dict) -> bool:
        session = boto3.Session(
            aws_access_key_id=validator_ctx.get("aws_access_key_id"),
            aws_secret_access_key=validator_ctx.get("aws_secret_access_key"),
            region_name=spec.params.get("region", "us-east-1")
        )
        logs = session.client("logs")
        try:
            resp = logs.describe_log_groups(logGroupNamePrefix=spec.params["name"])
            groups = resp.get("logGroups", [])
            return any(g["logGroupName"] == spec.params["name"] for g in groups)
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="aws",
        kind="aws.cloudwatchloggroup.create",
        params={"name": log_group_name, "region": region, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
