# ac_tasks/terraform/minimal_vm.py
from __future__ import annotations

from alphacore.benchmark.core.task.models import TerraformTask, TaskSpec, Invariant


def build_task(
    validator_sa: str,
) -> TerraformTask:
    """
    First Terraform task:
      - GCP compute instance
      - name = minimal-vm
      - zone = us-central1-a
      - machine_type = e2-micro

    project_id is chosen by the miner and returned in meta.json,
    so we do NOT pin it in the invariants.
    """
    import random

    # List of cheap GCP machine types
    cheap_machine_types = ["e2-micro", "e2-small", "e2-medium"]
    # List of popular GCP zones
    popular_zones = [
        "us-central1-a",
        "us-central1-b",
        "us-central1-c",
        "us-central1-f",
        "us-east1-b",
        "us-east1-c",
        "us-east1-d",
        "europe-west1-b",
        "europe-west1-c",
        "europe-west1-d",
    ]

    task_id = TaskSpec.new_id()
    nonce = TaskSpec.new_nonce()

    # VM name includes nonce for uniqueness
    name = f"vm-{nonce}"
    zone = random.choice(popular_zones)
    machine_type = random.choice(cheap_machine_types)

    inv = Invariant(
        resource_type="google_compute_instance",
        match={
            "values.name": name,
            "values.zone": zone,
            "values.machine_type": machine_type,
            # "values.labels.ac_task_id": task_id,
            # "values.labels.ac_nonce": nonce,
        },
    )

    spec = TaskSpec(
        version="v0",
        task_id=task_id,
        nonce=nonce,
        kind="virtual machine",
        invariants=[inv],
    )

    return TerraformTask(
        engine="terraform",
        provider="gcp",
        validator_sa=validator_sa,
        spec=spec,
    )
