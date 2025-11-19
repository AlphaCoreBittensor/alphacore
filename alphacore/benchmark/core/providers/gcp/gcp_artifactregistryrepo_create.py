
from alphacore.benchmark.core.models import ACTaskSpec, VerifyPlan
from alphacore.benchmark.core.policy import DEFAULT_POLICY
import time
from google.cloud import artifactregistry_v1

def gcp_artifactregistryrepo_create(task_id: str, params: dict) -> ACTaskSpec:
    name = params.get("name", f"projects/default-project/locations/us-central1/repositories/ac-repo-{task_id}")
    location = params.get("location", "us-central1")
    format_ = params.get("format", "DOCKER")
    project = params.get("projectId", "default-project")
    verify = VerifyPlan(
        kind="gcp.readback.get",
        steps=[{"op": "artifactregistry.projects.locations.repositories.get", "name": name}],
    )
    def verify_fn(spec, validator_ctx):
        if not artifactregistry_v1:
            return False
        try:
            client = artifactregistry_v1.ArtifactRegistryClient()
            repo = client.get_repository(name=spec.params["name"])
            return repo.format.name == spec.params["format"]
        except Exception:
            return False
    spec = ACTaskSpec(
        task_id=task_id,
        provider="gcp",
        kind="gcp.artifactregistryrepo.create",
        params={"name": name, "location": location, "format": format_, "projectId": project, "labels": params.get("labels", {})},
        policy=DEFAULT_POLICY,
        verify_plan=verify,
        deadline_ts=time.time() + 900,
        cost_tier="low",
        verify_fn=verify_fn
    )
    return spec
