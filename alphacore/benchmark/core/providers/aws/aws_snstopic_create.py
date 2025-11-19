
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
import boto3

def aws_snstopic_create(task_id: str, params: dict) -> ACTaskSpec:
    topic_name = params.get("name", f"ac-topic-{task_id}")
    region = params.get("region", "us-east-1")
    verify = VerifyPlan(
        kind="aws.readback.describe",
        steps=[{"op": "sns.get_topic_attributes", "name": topic_name}],
    )
    def verify_fn(spec: ACTaskSpec, validator_ctx: dict) -> bool:
        session = boto3.Session(
            aws_access_key_id=validator_ctx.get("aws_access_key_id"),
            aws_secret_access_key=validator_ctx.get("aws_secret_access_key"),
            region_name=spec.params.get("region", "us-east-1")
        )
        sns = session.client("sns")
        try:
            # Find topic ARN by listing topics and matching name
            topics = sns.list_topics()["Topics"]
            arn = next((t["TopicArn"] for t in topics if spec.params["name"] in t["TopicArn"]), None)
            if not arn:
                return False
            attrs = sns.get_topic_attributes(TopicArn=arn)
            return 'TopicArn' in attrs.get('Attributes', {})
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="aws",
        kind="aws.snstopic.create",
        params={"name": topic_name, "region": region, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
