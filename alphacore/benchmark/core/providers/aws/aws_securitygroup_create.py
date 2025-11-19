
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
import boto3

def aws_securitygroup_create(task_id: str, params: dict) -> ACTaskSpec:
    group_name = params.get("name", f"ac-sg-{task_id}")
    vpc_id = params.get("vpc_id", None)
    region = params.get("region", "us-east-1")
    verify = VerifyPlan(
        kind="aws.readback.describe",
        steps=[{"op": "ec2.describe_security_groups", "name": group_name}],
    )
    def verify_fn(spec: ACTaskSpec, validator_ctx: dict) -> bool:
        session = boto3.Session(
            aws_access_key_id=validator_ctx.get("aws_access_key_id"),
            aws_secret_access_key=validator_ctx.get("aws_secret_access_key"),
            region_name=spec.params.get("region", "us-east-1")
        )
        ec2 = session.client("ec2")
        try:
            resp = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [spec.params["name"]]}])
            groups = resp.get("SecurityGroups", [])
            return any(g.get("GroupName") == spec.params["name"] and (not spec.params.get("vpc_id") or g.get("VpcId") == spec.params["vpc_id"]) for g in groups)
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="aws",
        kind="aws.securitygroup.create",
        params={"name": group_name, "vpc_id": vpc_id, "region": region, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
