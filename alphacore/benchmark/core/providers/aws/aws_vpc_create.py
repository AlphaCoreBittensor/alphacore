
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
import boto3

def aws_vpc_create(task_id: str, params: dict) -> ACTaskSpec:
    vpc_cidr = params.get("cidr_block", "10.0.0.0/16")
    region = params.get("region", "us-east-1")
    verify = VerifyPlan(
        kind="aws.readback.describe",
        steps=[{"op": "ec2.describe_vpcs", "cidr_block": vpc_cidr}],
    )
    def verify_fn(spec: ACTaskSpec, validator_ctx: dict) -> bool:
        session = boto3.Session(
            aws_access_key_id=validator_ctx.get("aws_access_key_id"),
            aws_secret_access_key=validator_ctx.get("aws_secret_access_key"),
            region_name=spec.params.get("region", "us-east-1")
        )
        ec2 = session.client("ec2")
        try:
            resp = ec2.describe_vpcs()
            vpcs = resp.get("Vpcs", [])
            return any(vpc.get("CidrBlock") == spec.params["cidr_block"] for vpc in vpcs)
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="aws",
        kind="aws.vpc.create",
        params={"cidr_block": vpc_cidr, "region": region, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
