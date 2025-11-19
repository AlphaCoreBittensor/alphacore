
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
import boto3

def aws_iaminstanceprofile_create(task_id: str, params: dict) -> ACTaskSpec:
    profile_name = params.get("name", f"ac-profile-{task_id}")
    region = params.get("region", "us-east-1")
    verify = VerifyPlan(
        kind="aws.readback.describe",
        steps=[{"op": "iam.get_instance_profile", "name": profile_name}],
    )
    def verify_fn(spec: ACTaskSpec, validator_ctx: dict) -> bool:
        session = boto3.Session(
            aws_access_key_id=validator_ctx.get("aws_access_key_id"),
            aws_secret_access_key=validator_ctx.get("aws_secret_access_key"),
            region_name=spec.params.get("region", "us-east-1")
        )
        iam = session.client("iam")
        try:
            resp = iam.get_instance_profile(InstanceProfileName=spec.params["name"])
            return 'InstanceProfile' in resp
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="aws",
        kind="aws.iaminstanceprofile.create",
        params={"name": profile_name, "region": region, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
