
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
import boto3

def aws_eventbridge_rule_create(task_id: str, params: dict) -> ACTaskSpec:
    rule_name = params.get("name", f"ac-rule-{task_id}")
    event_bus = params.get("event_bus", "default")
    region = params.get("region", "us-east-1")
    verify = VerifyPlan(
        kind="aws.readback.describe",
        steps=[{"op": "events.describe_rule", "name": rule_name}],
    )
    def verify_fn(spec: ACTaskSpec, validator_ctx: dict) -> bool:
        session = boto3.Session(
            aws_access_key_id=validator_ctx.get("aws_access_key_id"),
            aws_secret_access_key=validator_ctx.get("aws_secret_access_key"),
            region_name=spec.params.get("region", "us-east-1")
        )
        events = session.client("events")
        try:
            rule = events.describe_rule(Name=spec.params["name"], EventBusName=spec.params.get("event_bus", "default"))
            return rule.get("Name") == spec.params["name"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="aws",
        kind="aws.eventbridge_rule.create",
        params={"name": rule_name, "event_bus": event_bus, "region": region, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
