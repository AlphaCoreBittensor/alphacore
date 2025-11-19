
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
import boto3

def aws_s3bucket_create(task_id: str, params: dict) -> ACTaskSpec:
    bucket_name = params.get("name", f"ac-bucket-{task_id}")
    region = params.get("region", "us-east-1")
    verify = VerifyPlan(
        kind="aws.readback.head",
        steps=[{"op": "s3.head_bucket", "name": bucket_name}, {"op": "s3.get_bucket_location", "name": bucket_name}],
    )
    def verify_fn(spec: ACTaskSpec, validator_ctx: dict) -> bool:
        session = boto3.Session(
            aws_access_key_id=validator_ctx.get("aws_access_key_id"),
            aws_secret_access_key=validator_ctx.get("aws_secret_access_key"),
            region_name=spec.params.get("region", "us-east-1")
        )
        s3 = session.client("s3")
        try:
            s3.head_bucket(Bucket=spec.params["name"])
            loc = s3.get_bucket_location(Bucket=spec.params["name"])
            return loc.get("LocationConstraint") == spec.params["region"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="aws",
        kind="aws.s3bucket.create",
        params={"name": bucket_name, "region": region, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
