"""
Task evaluation mixin for scoring miner responses.

Handles evaluation of task responses and computation of rewards.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import hashlib
import tarfile
import tempfile
import time
import zipfile
from dataclasses import asdict
from typing import List, Optional
import math

import bittensor as bt

from modules.evaluation.evaluator import Evaluator
from modules.models import ACTaskSpec
from subnet.protocol import TaskSynapse
from subnet.validator.validation_client import ValidationAPIClient, ValidationAPIClientPool
from subnet.validator.config import (
    VALIDATION_API_ENABLED,
    VALIDATION_API_ENDPOINT,
    VALIDATION_API_TIMEOUT,
    VALIDATION_API_RETRIES,
)
from subnet.validator.config import API_SCORE_WEIGHT, LATENCY_SCORE_WEIGHT, LATENCY_SCORE_GAMMA
from subnet.validator.config import VALIDATION_CONCURRENCY, LATENCY_TIE_EPSILON_S, LATENCY_TIE_PENALTY_MAX
from subnet.validator.task_ledger import TaskLedger


class TaskEvaluationMixin:
    """Evaluate miner responses and compute rewards."""

    def __init__(self, **kwargs):
        """Initialize task evaluation state."""
        super().__init__(**kwargs)  # Pass to next in MRO
        self._evaluator: Optional[Evaluator] = None
        self._evaluation_start: Optional[float] = None
        self._scores_by_round: dict = {}  # round_id -> {uid: score}
        self._validation_results_by_round: dict = {}  # round_id -> {uid: {task_id: dict}}
        self._validation_attempts_by_round: dict = {}  # round_id -> {uid: {task_id: dict}}
        self._validation_client: Optional[ValidationAPIClient] = None
        self._validation_client_pool: Optional[ValidationAPIClientPool] = None
        self._task_ledger: TaskLedger = getattr(self, "_task_ledger", TaskLedger())

    def _ledger(self, event: str, payload: dict) -> None:
        try:
            self._task_ledger.write(event, payload)
        except Exception:
            return

    async def _run_consensus_phase(
        self, tasks: List[ACTaskSpec], responses: dict
    ) -> dict:
        """
        Evaluate task responses and compute scores.

        Uses the validation API if enabled, falls back to local evaluation.

        Args:
            tasks: Original task specifications
            responses: Dictionary mapping uid -> TaskSynapse response

        Returns:
            Dictionary mapping uid -> ACScore
        """
        self._evaluation_start = time.time()
        bt.logging.info(f" Evaluating {len(responses)} responses from targets")

        # Initialize evaluator if not already done
        if self._evaluator is None:
            self._evaluator = Evaluator()
            bt.logging.info("✓ Initialized Evaluator")

        scores_dict: dict[int, float] = {}
        api_scores_by_uid: dict[int, float] = {}
        api_counts_by_uid: dict[int, int] = {}
        round_id = self.get_current_round_id()
        validation_by_uid: dict[int, dict[str, dict]] = {}
        validation_attempts_by_uid: dict[int, dict[str, dict]] = {}
        try:
            self._ledger(
                "evaluation_start",
                {
                    "round_id": round_id,
                    "task_ids": [getattr(t, "task_id", None) for t in (tasks or [])],
                    "target_uids": [int(uid) for uid in (responses or {}).keys()],
                },
            )
        except Exception:
            pass

        try:
            # Evaluate miners concurrently, bounded by VALIDATION_CONCURRENCY.
            sem = asyncio.Semaphore(max(1, int(VALIDATION_CONCURRENCY)))

            async def _evaluate_uid(uid: int, by_task: dict) -> tuple[int, float, int, float, dict]:
                async with sem:
                    if not isinstance(by_task, dict):
                        return int(uid), 0, 0.0, 0.0, {}

                    uid_total_score = 0.0
                    uid_eval_count = 0
                    uid_api_sum = 0.0
                    uid_api_cnt = 0
                    uid_validation: dict[str, dict] = {}
                    uid_attempts: dict[str, dict] = {}

                    for task in tasks:
                        task_id = getattr(task, "task_id", None) if not isinstance(task, dict) else task.get("task_id")
                        if not task_id:
                            continue

                        synapse = by_task.get(task_id)
                        if synapse is None:
                            uid_attempts[str(task_id)] = {"status": "no_response"}
                            uid_total_score += 0.0
                            uid_eval_count += 1
                            continue

                        zip_b64 = getattr(synapse, "workspace_zip_b64", None)
                        if not zip_b64:
                            uid_attempts[str(task_id)] = {"status": "no_submission_zip"}
                            uid_total_score += 0.0
                            uid_eval_count += 1
                            continue

                        if isinstance(task, dict):
                            task_payload = dict(task)
                        else:
                            try:
                                from dataclasses import is_dataclass
                                task_payload = asdict(task) if is_dataclass(task) else {"task_id": task_id}
                            except Exception:
                                task_payload = {"task_id": task_id}

                        # Build the *validator-side* task.json which the sandbox uses for invariant checks.
                        # IMPORTANT: The sandbox validator expects invariants at top-level (see validate_task()).
                        validation_task_json: Optional[dict] = None
                        invariants_count = 0
                        # Preferred: use the validator's remembered per-round task.json (never trust miner payload).
                        try:
                            remembered = None
                            if hasattr(self, "get_validation_task_json"):
                                remembered = self.get_validation_task_json(round_id, task_id)  # type: ignore[attr-defined]
                            if isinstance(remembered, dict):
                                validation_task_json = remembered
                        except Exception:
                            pass
                        try:
                            params = task_payload.get("params") if isinstance(task_payload, dict) else None
                            if isinstance(params, dict):
                                validation_task_json = params.get("validation_task_json") or params.get("task_json")
                                if not isinstance(validation_task_json, dict):
                                    # Preferred: TaskGenerationPipeline embeds the canonical task schema under params.task.
                                    candidate = params.get("task")
                                    if isinstance(candidate, dict):
                                        validation_task_json = candidate
                        except Exception:
                            validation_task_json = None
                        if not isinstance(validation_task_json, dict):
                            # Fallback: try extracting invariants from the ACTaskSpec payload shape.
                            extracted_invariants = []
                            try:
                                params = task_payload.get("params") if isinstance(task_payload, dict) else None
                                if isinstance(params, dict):
                                    task_obj = params.get("task") or {}
                                    if isinstance(task_obj, dict):
                                        extracted_invariants = task_obj.get("invariants") or []
                            except Exception:
                                extracted_invariants = []
                            validation_task_json = {
                                "task_id": str(task_id),
                                "invariants": extracted_invariants if isinstance(extracted_invariants, list) else [],
                            }

                        # Ensure task_id is present and invariant count is tracked for debugging.
                        try:
                            if "task_id" not in validation_task_json:
                                validation_task_json["task_id"] = str(task_id)
                            if "miner_uid" not in validation_task_json:
                                validation_task_json["miner_uid"] = int(uid)
                            invariants = validation_task_json.get("invariants") if isinstance(validation_task_json, dict) else None
                            invariants_count = len(invariants) if isinstance(invariants, list) else 0
                        except Exception:
                            invariants_count = 0
                        if invariants_count <= 0:
                            uid_attempts[str(task_id)] = {
                                "status": "missing_invariants",
                                "error": "validator_task_json_missing_invariants",
                            }
                            uid_total_score += 0.0
                            uid_eval_count += 1
                            continue

                        score_value: Optional[float] = None
                        if not VALIDATION_API_ENABLED:
                            uid_attempts[str(task_id)] = {"status": "api_disabled"}
                            score_value = 1.0
                        else:
                            if self._validation_client is None:
                                self._validation_client = ValidationAPIClient(
                                    endpoint=VALIDATION_API_ENDPOINT,
                                    timeout=VALIDATION_API_TIMEOUT,
                                    max_retries=VALIDATION_API_RETRIES,
                                )
                                await self._validation_client.connect()

                            if await self._validation_client.health_check():
                                tmp_dir = tempfile.mkdtemp(prefix=f"alphacore-eval-{uid}-{task_id}-")
                                zip_path = os.path.join(tmp_dir, "workspace.zip")
                                try:
                                    raw = base64.b64decode(zip_b64)
                                    with open(zip_path, "wb") as f:
                                        f.write(raw)
                                    try:
                                        debug_validation = os.getenv("ALPHACORE_VALIDATION_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
                                        task_hash = ""
                                        try:
                                            task_hash = hashlib.sha256(
                                                json.dumps(validation_task_json, sort_keys=True, ensure_ascii=True).encode("utf-8")
                                            ).hexdigest()
                                        except Exception:
                                            task_hash = ""
                                        if debug_validation:
                                            bt.logging.info(
                                                f"VALIDATION_SUBMIT uid={uid} task_id={task_id} "
                                                f"invariants={invariants_count} "
                                                f"zip_sha256={getattr(synapse, 'workspace_zip_sha256', None)} "
                                                f"task_sha256={task_hash}"
                                            )
                                        validation_response_obj = await self._validation_client.submit_validation(
                                            workspace_zip_path=zip_path,
                                            task_json=validation_task_json,
                                            task_id=task_id,
                                            timeout_s=max(1, int(VALIDATION_API_TIMEOUT / 2)),
                                            net_checks=False,
                                            stream_log=False,
                                        )
                                        if validation_response_obj is not None:
                                            score_value = float(getattr(validation_response_obj, "score", 0.0))
                                            try:
                                                bt.logging.info(
                                                    f"VALIDATION_JOB_SUBMITTED uid={int(uid)} task_id={str(task_id)} "
                                                    f"job_id={getattr(validation_response_obj, 'job_id', '')} "
                                                    f"log_path={getattr(validation_response_obj, 'log_path', '')} "
                                                    f"log_url={getattr(validation_response_obj, 'log_url', '')} "
                                                    f"submission_path={getattr(validation_response_obj, 'submission_path', '')} "
                                                    f"invariants={int(invariants_count)} score={float(score_value):.4f}"
                                                )
                                            except Exception:
                                                pass
                                            uid_validation[str(task_id)] = {
                                                "job_id": getattr(validation_response_obj, "job_id", ""),
                                                "task_id": getattr(validation_response_obj, "task_id", None),
                                                "result": getattr(validation_response_obj, "result", {}) or {},
                                                "log_url": getattr(validation_response_obj, "log_url", ""),
                                                "log_path": getattr(validation_response_obj, "log_path", ""),
                                                "submission_path": getattr(validation_response_obj, "submission_path", ""),
                                                "tap": getattr(validation_response_obj, "tap", None),
                                            }
                                            uid_attempts[str(task_id)] = {
                                                "status": "validated",
                                                "score": float(score_value),
                                                "job_id": getattr(validation_response_obj, "job_id", ""),
                                                "log_path": getattr(validation_response_obj, "log_path", ""),
                                                "invariants": int(invariants_count),
                                                "zip_sha256": getattr(synapse, "workspace_zip_sha256", None),
                                                "task_sha256": task_hash or None,
                                            }
                                        else:
                                            uid_attempts[str(task_id)] = {"status": "api_returned_none"}
                                    except Exception as exc:
                                        uid_attempts[str(task_id)] = {
                                            "status": "api_error",
                                            "error_type": type(exc).__name__,
                                            "error": str(exc),
                                            "invariants": int(invariants_count),
                                        }
                                finally:
                                    shutil.rmtree(tmp_dir, ignore_errors=True)
                            else:
                                uid_attempts[str(task_id)] = {"status": "api_unhealthy"}

                        if score_value is None:
                            # If API is disabled or unavailable, fail closed.
                            score_value = 0.0 if VALIDATION_API_ENABLED else 1.0

                        uid_total_score += float(score_value)
                        uid_eval_count += 1
                        uid_api_sum += float(score_value)
                        uid_api_cnt += 1

                    avg = (uid_total_score / uid_eval_count) if uid_eval_count > 0 else 0.0
                    # Store attempts for the outer scope (best-effort).
                    try:
                        validation_attempts_by_uid[int(uid)] = dict(uid_attempts)
                    except Exception:
                        pass
                    return int(uid), int(uid_eval_count), float(uid_api_sum), float(avg), uid_validation

            eval_tasks = [
                _evaluate_uid(int(uid), by_task)
                for uid, by_task in (responses or {}).items()
            ]
            results = await asyncio.gather(*eval_tasks) if eval_tasks else []

            for uid, eval_count, api_sum, avg_score, uid_validation in results:
                scores_dict[int(uid)] = float(avg_score) if eval_count > 0 else 0.0
                api_scores_by_uid[int(uid)] = float(api_sum)
                api_counts_by_uid[int(uid)] = int(eval_count)
                if uid_validation:
                    validation_by_uid[int(uid)] = uid_validation

                if eval_count > 0:
                    bt.logging.info(
                        f"✓ UID {uid}: avg_score={float(avg_score):.4f} "
                        f"({int(eval_count)}/{len(tasks)} tasks)"
                    )
                else:
                    bt.logging.info(f"✗ UID {uid}: no valid evaluations")

            evaluation_time = time.time() - self._evaluation_start
            valid_scores = sum(1 for s in scores_dict.values() if s > 0)
            bt.logging.info(
                f"✓ Evaluation completed in {evaluation_time:.2f}s | "
                f"Valid scores: {valid_scores}/{len(scores_dict)}"
            )

            # Store per-task validation results for cleanup/inspection.
            if validation_by_uid:
                self._validation_results_by_round[round_id] = validation_by_uid
            # Always store validation attempts, even when the API couldn't validate.
            if validation_attempts_by_uid:
                self._validation_attempts_by_round[round_id] = validation_attempts_by_uid

            # Combine API score with relative latency score.
            from subnet.validator.config import LATENCY_SCORING_ENABLED
            final_scores = dict(scores_dict)
            if LATENCY_SCORING_ENABLED and final_scores:
                raw_latencies = self.get_latencies(round_id)
                # Dispatch stores per-task latencies keyed by (uid, task_id).
                # Convert to per-uid average.
                latencies: dict[int, float] = {}
                if isinstance(raw_latencies, dict):
                    accum: dict[int, float] = {}
                    counts: dict[int, int] = {}
                    for key, value in raw_latencies.items():
                        if isinstance(key, tuple) and len(key) == 2:
                            uid_key = key[0]
                        else:
                            uid_key = key
                        try:
                            uid_int = int(uid_key)
                        except Exception:
                            continue
                        try:
                            v = float(value)
                        except Exception:
                            v = 0.0
                        accum[uid_int] = accum.get(uid_int, 0.0) + v
                        counts[uid_int] = counts.get(uid_int, 0) + 1
                    for uid_int, total in accum.items():
                        denom = max(1, int(counts.get(uid_int, 1)))
                        latencies[uid_int] = total / float(denom)

                # Relative latency scores are computed across miners that have a score.
                uid_list = [int(uid) for uid in final_scores.keys()]
                observed_latency_values = [
                    float(latencies[uid]) for uid in uid_list if uid in latencies
                ]
                min_latency = min(observed_latency_values) if observed_latency_values else 0.0
                max_latency = max(observed_latency_values) if observed_latency_values else min_latency
                latency_range = float(max_latency - min_latency)
                denom = max(1e-9, latency_range)

                api_w = float(API_SCORE_WEIGHT)
                lat_w = float(LATENCY_SCORE_WEIGHT)
                total_w = api_w + lat_w
                if total_w <= 0:
                    api_w, lat_w, total_w = 1.0, 0.0, 1.0
                api_w /= total_w
                lat_w /= total_w

                gamma = max(0.0001, float(LATENCY_SCORE_GAMMA))
                tie_eps = max(0.0, float(LATENCY_TIE_EPSILON_S))
                tie_penalty = max(0.0, min(1.0, float(LATENCY_TIE_PENALTY_MAX)))

                tie_mode = bool(len(observed_latency_values) >= 2 and latency_range <= tie_eps and tie_penalty > 0.0)
                tie_latency_scores: dict[int, float] = {}
                if tie_mode:
                    uid_sorted = sorted(
                        uid_list,
                        key=lambda u: (float(latencies.get(int(u), max_latency)), int(u)),
                    )
                    n = len(uid_sorted)
                    if n > 1:
                        for rank, uid in enumerate(uid_sorted):
                            frac = float(rank) / float(n - 1)
                            tie_latency_scores[int(uid)] = max(0.0, min(1.0, 1.0 - frac * tie_penalty))
                    else:
                        tie_latency_scores[int(uid_sorted[0])] = 1.0
                    bt.logging.info(
                        f"⏱️ Latency tie-spread enabled: range={latency_range:.6f}s <= eps={tie_eps:.6f}s "
                        f"penalty_max={tie_penalty:.3f}"
                    )

                combined: dict[int, float] = {}
                for uid in uid_list:
                    api_sum = float(api_scores_by_uid.get(uid, 0.0))
                    api_cnt = max(1, int(api_counts_by_uid.get(uid, 0)))
                    api_avg = api_sum / float(api_cnt)

                    # If the validator API score is 0, the overall score must be 0 (fail closed),
                    # regardless of how fast the miner responded.
                    if api_avg <= 0.0:
                        combined[uid] = 0.0
                        bt.logging.info(
                            f"📊 UID {uid}: api=0.0000 -> final=0.0000 (latency ignored)"
                        )
                        continue

                    if uid in latencies:
                        latency = float(latencies.get(uid, 0.0))
                    else:
                        latency = float(max_latency)
                    if tie_mode:
                        latency_score = float(tie_latency_scores.get(int(uid), 1.0))
                    else:
                        delta = (latency - min_latency) / denom
                        delta = max(0.0, min(1.0, float(delta)))
                        latency_score = float((1.0 - delta) ** gamma)

                    combined_score = api_w * float(api_avg) + lat_w * float(latency_score)
                    combined[uid] = max(0.0, min(1.0, float(combined_score)))

                    bt.logging.info(
                        f"📊 UID {uid}: api={api_avg:.4f} latency={latency:.6f}s ({latency*1000.0:.1f}ms) "
                        f"lat_score={latency_score:.4f} final={combined[uid]:.4f} "
                        f"(weights api={api_w:.2f} lat={lat_w:.2f})"
                    )

                final_scores = combined

            self._scores_by_round[round_id] = final_scores
            try:
                # Recompute per-uid average latency for reporting (same logic as above).
                raw_latencies = self.get_latencies(round_id)
                latencies: dict[int, float] = {}
                if isinstance(raw_latencies, dict):
                    accum: dict[int, float] = {}
                    counts: dict[int, int] = {}
                    for key, value in raw_latencies.items():
                        if isinstance(key, tuple) and len(key) == 2:
                            uid_key = key[0]
                        else:
                            uid_key = key
                        try:
                            uid_int = int(uid_key)
                        except Exception:
                            continue
                        try:
                            v = float(value)
                        except Exception:
                            v = 0.0
                        accum[uid_int] = accum.get(uid_int, 0.0) + v
                        counts[uid_int] = counts.get(uid_int, 0) + 1
                    for uid_int, total in accum.items():
                        denom = max(1, int(counts.get(uid_int, 1)))
                        latencies[uid_int] = total / float(denom)

                self._ledger(
                    "evaluation_complete",
                    {
                        "round_id": round_id,
                        "evaluation_time_s": float(evaluation_time),
                        "valid_scores": int(valid_scores),
                        "validation_attempts": validation_attempts_by_uid,
                        "scores": [
                            {
                                "uid": int(uid),
                                "api_sum": float(api_scores_by_uid.get(int(uid), 0.0)),
                                "api_count": int(api_counts_by_uid.get(int(uid), 0)),
                                "avg_latency_s": float(latencies.get(int(uid), 0.0)),
                                "final_score": float(final_scores.get(int(uid), 0.0)),
                                "validation": validation_by_uid.get(int(uid), {}),
                            }
                            for uid in sorted(final_scores.keys(), key=lambda u: int(u))
                        ],
                    },
                )
            except Exception:
                pass
            return final_scores

        except Exception as e:
            bt.logging.error(f"✗ Consensus phase failed: {e}")
            raise

    def get_validation_results(self, round_id: Optional[str] = None) -> dict:
        if round_id is None:
            round_id = self.get_current_round_id()
        return self._validation_results_by_round.get(round_id, {})

    def get_validation_attempts(self, round_id: Optional[str] = None) -> dict:
        if round_id is None:
            round_id = self.get_current_round_id()
        return self._validation_attempts_by_round.get(round_id, {})

    # ------------------------------------------------------------------ #
    # Validation API Scoring
    # ------------------------------------------------------------------ #

    async def _score_with_validation_api(
        self,
        uid: int,
        task: dict,
        result: dict,
        bundle_dir: str,
    ) -> Optional:
        """
        Score a task using the external Validation API.

        Args:
            uid: Miner UID
            task: Task specification dict
            result: Miner's result dict
            bundle_dir: Path to unpacked bundle directory

        Returns:
            ACScore if successful, None if validation API fails or returns error
        """
        try:
            # Get task_id for tracking
            task_id = task.get("task_id", f"unknown_{uid}")

            # Check if validation API is healthy
            if self._validation_client is None:
                self._validation_client = ValidationAPIClient(
                    endpoint=VALIDATION_API_ENDPOINT,
                    timeout=VALIDATION_API_TIMEOUT,
                    max_retries=VALIDATION_API_RETRIES,
                )
                await self._validation_client.connect()

            # Check health
            is_healthy = await self._validation_client.health_check()
            if not is_healthy:
                bt.logging.warning(
                    f"Validation API not healthy, falling back to local evaluation for UID {uid}"
                )
                return None

            task_payload = dict(task)
            if "miner_uid" not in task_payload:
                task_payload["miner_uid"] = int(uid)

            # Create workspace zip from bundle_dir
            workspace_zip = await self._create_workspace_zip(bundle_dir, task_id)
            if not workspace_zip:
                bt.logging.warning(
                    f"Failed to create workspace zip for UID {uid} task {task_id}"
                )
                return None

            try:
                # Submit validation request
                validation_response = await self._validation_client.submit_validation(
                    workspace_zip_path=workspace_zip,
                    task_json=task_payload,
                    task_id=task_id,
                    timeout_s=int(VALIDATION_API_TIMEOUT / 2),  # seconds - Use half of total timeout
                    net_checks=False,
                    stream_log=False,
                )

                if validation_response is None:
                    bt.logging.warning(
                        f"Validation API returned None for UID {uid} task {task_id}"
                    )
                    return None

                # Extract score from response
                score_value = validation_response.score
                status = validation_response.status

                bt.logging.debug(
                    f"Validation API result for UID {uid} task {task_id}: "
                    f"status={status}, score={score_value:.4f}"
                )

                # Create a compatible score object
                # The local Evaluator returns an object with a .score attribute
                class APIScore:
                    def __init__(self, score: float):
                        self.score = float(score)

                return APIScore(score_value)

            finally:
                # Clean up workspace zip
                try:
                    if workspace_zip and os.path.exists(workspace_zip):
                        os.unlink(workspace_zip)
                except Exception as e:
                    bt.logging.debug(f"Failed to clean up workspace zip: {e}")

        except Exception as e:
            bt.logging.warning(
                f"Validation API scoring failed for UID {uid}: {e}. "
                f"Will use local evaluation."
            )
            return None

    async def _create_workspace_zip(self, bundle_dir: str, task_id: str) -> Optional[str]:
        """
        Create a workspace zip file from the bundle directory.

        Args:
            bundle_dir: Path to the bundle directory
            task_id: Task ID for naming

        Returns:
            Path to created zip file, or None if failed
        """
        try:
            tmp_file = tempfile.NamedTemporaryFile(
                delete=False, suffix=f"_{task_id}.zip"
            )
            tmp_zip_path = tmp_file.name
            tmp_file.close()

            with zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(bundle_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, bundle_dir)
                        zf.write(file_path, arcname)

            return tmp_zip_path

        except Exception as e:
            bt.logging.debug(f"Failed to create workspace zip: {e}")
            return None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _normalize_task_result(self, task_result) -> dict:
        """Coerce miner task_result into a dict, preserving archive hints."""
        result_payload = {}

        if isinstance(task_result, dict):
            result_payload = dict(task_result)
        elif hasattr(task_result, "__dict__"):
            result_payload = dict(task_result.__dict__)
        else:
            result_payload = {"raw_result": task_result}

        # Preserve common archive hints if they exist as attributes
        for key in ("archive_path", "archive_b64", "archive_bytes"):
            if key not in result_payload and hasattr(task_result, key):
                result_payload[key] = getattr(task_result, key)

        return result_payload

    def _extract_bundle_dir(self, result_payload: dict) -> Optional[str]:
        """Extract or locate bundle dir from archive path/bytes provided by miner."""
        # If miner provided an already-unpacked bundle_dir, trust it.
        bundle_dir = result_payload.get("bundle_dir")
        if bundle_dir and os.path.isdir(bundle_dir):
            return bundle_dir

        archive_path = result_payload.get("archive_path")
        if archive_path and os.path.isfile(archive_path):
            unpacked = self._unpack_archive(archive_path)
            if unpacked:
                return unpacked

        # Decode inline archive bytes (base64 or raw bytes)
        archive_bytes = result_payload.get("archive_bytes")
        if archive_bytes is None and result_payload.get("archive_b64"):
            try:
                archive_bytes = base64.b64decode(result_payload["archive_b64"])
            except Exception as exc:
                bt.logging.debug(f"Failed to decode archive_b64: {exc}")

        if archive_bytes:
            try:
                tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
                tmp_file.write(archive_bytes)
                tmp_file.close()
                unpacked = self._unpack_archive(tmp_file.name)
                if unpacked:
                    return unpacked
            except Exception as exc:
                bt.logging.debug(f"Failed to unpack archive_bytes: {exc}")
            finally:
                try:
                    os.unlink(tmp_file.name)
                except Exception:
                    pass

        return None

    def _unpack_archive(self, archive_path: str) -> Optional[str]:
        """Unpack zip or tar archive to a temp dir and return the dir path."""
        tmp_dir = tempfile.mkdtemp(prefix="ac_bundle_")

        try:
            if zipfile.is_zipfile(archive_path):
                with zipfile.ZipFile(archive_path, "r") as zf:
                    zf.extractall(tmp_dir)
                return tmp_dir

            try:
                with tarfile.open(archive_path, "r:*") as tf:
                    tf.extractall(tmp_dir)
                return tmp_dir
            except tarfile.ReadError:
                pass

        except Exception as exc:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            bt.logging.debug(f"Failed to unpack archive {archive_path}: {exc}")
            return None

        # If neither zip nor tar worked, clean up and bail
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None

    def get_scores(self, round_id: Optional[str] = None) -> dict:
        """Get scores for a round."""
        if round_id is None:
            round_id = self.get_current_round_id()
        return self._scores_by_round.get(round_id, {})

    def clear_scores(self, round_id: Optional[str] = None) -> None:
        """Clear scores for a round."""
        if round_id is None:
            round_id = self.get_current_round_id()
        if round_id in self._scores_by_round:
            del self._scores_by_round[round_id]

    async def close_validation_client(self) -> None:
        """Close the validation API client."""
        if self._validation_client:
            await self._validation_client.disconnect()
            self._validation_client = None
        if self._validation_client_pool:
            await self._validation_client_pool.stop()
            self._validation_client_pool = None
