
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
import boto3

def aws_ssmparameter_create(task_id: str, params: dict) -> ACTaskSpec:
    param_name = params.get("name", f"ac-param-{task_id}")
    region = params.get("region", "us-east-1")
    tier = params.get("tier", "Standard")
    verify = VerifyPlan(
        kind="aws.readback.describe",
        steps=[{"op": "ssm.get_parameter", "name": param_name}, {"op": "ssm.describe_parameters", "name": param_name}],
    )
    def verify_fn(spec: ACTaskSpec, validator_ctx: dict) -> bool:
        session = boto3.Session(
            aws_access_key_id=validator_ctx.get("aws_access_key_id"),
            aws_secret_access_key=validator_ctx.get("aws_secret_access_key"),
            region_name=spec.params.get("region", "us-east-1")
        )
        ssm = session.client("ssm")
        try:
            param = ssm.get_parameter(Name=spec.params["name"])
            desc = ssm.describe_parameters(ParameterFilters=[{"Key": "Name", "Values": [spec.params["name"]]}])
            return param and desc.get('Parameters', [{}])[0].get('Tier', 'Standard') == spec.params['tier']
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="aws",
        kind="aws.ssmparameter.create",
        params={"name": param_name, "region": region, "tier": tier, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
