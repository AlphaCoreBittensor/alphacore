
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
from google.cloud import pubsub_v1

def gcp_pubsubsubscription_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"projects/default-project/subscriptions/ac-sub-{task_id}")
    topic = params.get("topic", f"projects/default-project/topics/ac-topic-{task_id}")
    project = params.get("projectId", "default-project")
    verify = VerifyPlan(
        kind="gcp.readback.get",
        steps=[{"op": "pubsub.projects.subscriptions.get", "name": name}],
    )
    def verify_fn(spec, validator_ctx):
        if not pubsub_v1:
            return False
        try:
            client = pubsub_v1.SubscriberClient()
            sub = client.get_subscription(request={"subscription": spec.params["name"]})
            return sub.topic == spec.params["topic"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="gcp",
        kind="gcp.pubsubsubscription.create",
        params={"name": name, "topic": topic, "projectId": project, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
