"""
Evaluation scaffolding for AlphaCore validators.

This module intentionally keeps the scoring logic lightweight while the
full verification pipeline is under construction.  Validators can plug the
`Evaluator` class into their loop to transform miner responses into `ACScore`
objects, and then convert those into reward weights via `get_rewards`.
"""

from __future__ import annotations

import os
import json
import subprocess
import logging

from typing import Any, Dict
from alphacore.benchmark.core.task.models import (
    ACScore,
)


class Evaluator:
    """
    Evaluator for Terraform tasks. Checks statefile, runs plan refresh, ensures no changes, checks statefile content, and matches invariants.
    """

    def evaluate(self, task: Dict[str, Any], response: Dict[str, Any]) -> ACScore:
        logger = logging.getLogger("Evaluator")
        task_spec = task.get("task") or task.get("spec", {})
        task_id = str(task.get("task_id") or task_spec.get("task_id") or "unknown")
        submit_reqs = task.get("submit_requirements", {})
        bundle_layout = submit_reqs.get("bundle_layout", {})
        statefile_rel = bundle_layout.get("state", "terraform.tfstate")
        bundle_dir = response.get("bundle_dir", "")
        statefile_path = os.path.join(bundle_dir, statefile_rel)
        logger.info(f"Checking for statefile at {statefile_path}")
        if not os.path.exists(statefile_path):
            logger.error("Statefile does not exist.")
            return ACScore(task_id=task_id, pass_fail=0)

        # 1. Run terraform plan -refresh-only (log output, but do not fail on changes)
        plan_refresh_cmd = ["terraform", "plan", "-refresh-only", "-no-color"]
        logger.info(f"Running: {' '.join(plan_refresh_cmd)} in {bundle_dir}")
        try:
            plan_refresh_proc = subprocess.run(
                plan_refresh_cmd,
                cwd=bundle_dir,
                capture_output=True,
                text=True,
                timeout=60,
                env=None,
            )
            logger.info(f"Terraform refresh-only output:\n{plan_refresh_proc.stdout}")
        except Exception as e:
            logger.error(f"Terraform plan -refresh-only failed: {e}")
            return ACScore(task_id=task_id, pass_fail=0)

        # Load statefile after refresh
        try:
            with open(statefile_path, "r") as f:
                state_data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load statefile after refresh: {e}")
            return ACScore(task_id=task_id, pass_fail=0)
        if not state_data or not state_data.get("resources"):
            logger.error("Statefile is empty or missing resources after refresh.")
            return ACScore(task_id=task_id, pass_fail=0)

        # 2. Run a normal terraform plan and ensure no changes
        plan_cmd = ["terraform", "plan", "-no-color"]
        logger.info(f"Running: {' '.join(plan_cmd)} in {bundle_dir}")
        try:
            plan_proc = subprocess.run(
                plan_cmd,
                cwd=bundle_dir,
                capture_output=True,
                text=True,
                timeout=60,
                env=None,
            )
            plan_out = plan_proc.stdout
            logger.info(f"Terraform plan output:\n{plan_out}")
            if "No changes." not in plan_out:
                logger.error("Terraform plan indicates changes would be made.")
                return ACScore(task_id=task_id, pass_fail=0)
        except Exception as e:
            logger.error(f"Terraform plan failed: {e}")
            return ACScore(task_id=task_id, pass_fail=0)

        spec_block = task.get("task") or task.get("spec", {})
        invariants = spec_block.get("invariants", [])
        logger.info(f"Checking {len(invariants)} invariants.")
        for inv in invariants:
            found = False
            for res in state_data.get("resources", []):
                if res.get("type") == inv.get("resource_type"):
                    values = res.get("instances", [{}])[0].get("attributes", {})
                    if all(
                        values.get(k.split(".")[-1]) == v
                        for k, v in inv.get("match", {}).items()
                    ):
                        found = True
                        break
            if not found:
                logger.error(f"Invariant not satisfied: {inv}")
                return ACScore(task_id=task_id, pass_fail=0)

        logger.info("All checks passed.")
        return ACScore(
            task_id=task_id,
            pass_fail=1,
            quality=1.0,
            timeliness=1.0,
            policy_adherence=1.0,
        )
