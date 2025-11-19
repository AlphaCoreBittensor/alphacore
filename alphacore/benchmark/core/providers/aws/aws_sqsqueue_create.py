
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
import boto3

def aws_sqsqueue_create(task_id: str, params: dict) -> ACTaskSpec:
    queue_name = params.get("name", f"ac-queue-{task_id}")
    region = params.get("region", "us-east-1")
    verify = VerifyPlan(
        kind="aws.readback.describe",
        steps=[{"op": "sqs.get_queue_attributes", "name": queue_name}],
    )
    def verify_fn(spec: ACTaskSpec, validator_ctx: dict) -> bool:
        session = boto3.Session(
            aws_access_key_id=validator_ctx.get("aws_access_key_id"),
            aws_secret_access_key=validator_ctx.get("aws_secret_access_key"),
            region_name=spec.params.get("region", "us-east-1")
        )
        sqs = session.client("sqs")
        try:
            url = sqs.get_queue_url(QueueName=spec.params["name"])['QueueUrl']
            attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=['QueueArn'])
            return 'QueueArn' in attrs.get('Attributes', {})
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="aws",
        kind="aws.sqsqueue.create",
        params={"name": queue_name, "region": region, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
