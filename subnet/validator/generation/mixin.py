"""
Task generation mixin for the validator.

Handles pre-generation and caching of tasks for each round with resume capability.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

import bittensor as bt

from modules.generation import TaskGenerationPipeline
from modules.models import ACTaskSpec
from subnet.validator.config import TASKS_PER_ROUND, PRE_GENERATED_TASKS, VERBOSE_TASK_LOGGING
from subnet.validator.task_ledger import TaskLedger


class TaskGenerationMixin:
    """Generate and cache tasks for each round with pre-generation pool."""

    @staticmethod
    def _is_placeholder_validator_sa(value: Optional[str]) -> bool:
        sa = (value or "").strip().lower()
        return sa in {
            "",
            "validator@example.com",
            "your-validator-sa@project.iam.gserviceaccount.com",
            "task-api@alphacore.local",
            "validator@alphacore.local",
        }

    def __init__(self, **kwargs):
        """Initialize task generation state with pre-gen pool."""
        super().__init__(**kwargs)  # Pass to next in MRO
        self._current_round_id: Optional[str] = None
        self._current_round_tasks: List[ACTaskSpec] = []
        # round_id -> task_id -> canonical task.json (contains top-level invariants)
        self._validation_task_json_by_round: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._generation_pipeline: Optional[TaskGenerationPipeline] = None
        self._task_generation_start: Optional[float] = None
        self._task_pool: List[ACTaskSpec] = []  # Pre-generated task pool
        self._pool_generation_time: Optional[float] = None
        self._task_generation_traces: Dict[str, Dict[str, Any]] = {}  # task_id -> trace metadata
        self._task_ledger: TaskLedger = getattr(self, "_task_ledger", TaskLedger())

    def _ledger(self, event: str, payload: Dict[str, Any]) -> None:
        try:
            self._task_ledger.write(event, payload)
        except Exception:
            return

    def _epoch_meta(self) -> Dict[str, Any]:
        """
        Best-effort epoch timing metadata for correlation across validators.
        """
        try:
            current_block = getattr(self, "block", None)
            tempo = getattr(self, "tempo", None)
            if current_block is None or tempo is None:
                return {}
            current_block_int = int(current_block)
            tempo_int = int(tempo)
            if current_block_int <= 0 or tempo_int <= 0:
                return {}
            blocks_into_epoch = current_block_int % tempo_int
            return {
                "block": current_block_int,
                "tempo": tempo_int,
                "epoch": current_block_int // tempo_int,
                "blocks_into_epoch": int(blocks_into_epoch),
                "epoch_fraction": float(blocks_into_epoch) / float(tempo_int),
                "blocks_until_epoch_end": int(tempo_int - blocks_into_epoch),
            }
        except Exception:
            return {}

    @staticmethod
    def _prompt_trace_summary(trace: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(trace, dict):
            return {}
        return {
            "success": bool(trace.get("success")) if "success" in trace else None,
            "fallback_used": bool(trace.get("fallback_used")) if "fallback_used" in trace else None,
            "model": trace.get("model"),
            "temperature": trace.get("temperature"),
            "final_attempt": trace.get("final_attempt"),
            "duration_s": trace.get("duration_s"),
            "error": trace.get("error"),
        }

    @staticmethod
    def _extract_task_invariants(task_spec: ACTaskSpec) -> List[Dict[str, Any]]:
        try:
            params = getattr(task_spec, "params", None)
            if not isinstance(params, dict):
                return []
            task_obj = params.get("task") or {}
            if not isinstance(task_obj, dict):
                return []
            invariants = task_obj.get("invariants") or []
            if isinstance(invariants, list):
                return invariants
        except Exception:
            return []
        return []

    @staticmethod
    def _extract_validation_task_json(task_spec: ACTaskSpec) -> Optional[Dict[str, Any]]:
        try:
            params = getattr(task_spec, "params", None)
            if not isinstance(params, dict):
                return None
            task_obj = params.get("task")
            if isinstance(task_obj, dict):
                return dict(task_obj)
        except Exception:
            return None
        return None

    def get_validation_task_json(self, round_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        """Return the canonical validator-side task.json for (round_id, task_id)."""
        try:
            by_round = self._validation_task_json_by_round.get(str(round_id)) or {}
            task_json = by_round.get(str(task_id))
            return dict(task_json) if isinstance(task_json, dict) else None
        except Exception:
            return None

    def _log_task_spec(self, task_spec: ACTaskSpec, trace: Dict[str, Any]) -> None:
        try:
            params = getattr(task_spec, "params", {}) or {}
            engine = params.get("engine") if isinstance(params, dict) else None
            validator_sa = params.get("validator_sa") if isinstance(params, dict) else None
            prompt = getattr(task_spec, "prompt", None)
            prompt_source = "unknown"
            if isinstance(trace, dict):
                if trace.get("fallback_used") is True:
                    prompt_source = "fallback"
                elif trace.get("success") is True:
                    prompt_source = "llm"
            payload = {
                "task_id": getattr(task_spec, "task_id", None),
                "provider": getattr(task_spec, "provider", None),
                "kind": getattr(task_spec, "kind", None),
                "engine": engine,
                "validator_sa": validator_sa,
                "prompt_source": prompt_source,
                "prompt": prompt,
                "invariants": self._extract_task_invariants(task_spec),
                "prompt_trace": self._prompt_trace_summary(trace),
                **self._epoch_meta(),
            }
            bt.logging.info(
                "TASK_SPEC\n"
                + json.dumps(
                    payload,
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=False,
                    default=str,
                )
            )
        except Exception:
            return

    async def _run_generation_phase(self, round_id: Optional[str] = None) -> List[ACTaskSpec]:
        """
        Generate tasks for the current round using pre-gen pool.

        Returns:
            List of ACTaskSpec tasks to be sent to miners
        """
        self._task_generation_start = time.time()

        # Initialize pipeline if not already done
        if self._generation_pipeline is None:
            # Validator service account/email used inside task specs (e.g. "grant access to ...").
            validator_sa = (os.getenv("ALPHACORE_VALIDATOR_SA") or "").strip() or None
            if validator_sa is None:
                env_profile = (os.getenv("ALPHACORE_ENV") or "local").strip().lower()
                if env_profile != "local":
                    raise RuntimeError(
                        "ALPHACORE_VALIDATOR_SA is required (set it to the validator's service account email)."
                    )
                validator_sa = "validator@alphacore.local"
            env_profile = (os.getenv("ALPHACORE_ENV") or "local").strip().lower()
            if env_profile != "local" and (
                self._is_placeholder_validator_sa(validator_sa) or "@" not in validator_sa
            ):
                raise RuntimeError(
                    "ALPHACORE_VALIDATOR_SA must be a real service account email (not a placeholder). "
                    "Re-run scripts/validator/process/launch_with_validation_api.sh with --gcp-creds-file "
                    "to infer it or pass --validator-sa explicitly."
                )
            self._generation_pipeline = TaskGenerationPipeline(validator_sa=validator_sa)
            bt.logging.info(f"✓ Initialized TaskGenerationPipeline for {validator_sa}")
            try:
                axon = getattr(self, "axon", None)
                config = getattr(self, "config", None)
                hotkey = None
                try:
                    if hasattr(self, "wallet") and self.wallet is not None and hasattr(self.wallet, "hotkey"):
                        hotkey = getattr(self.wallet.hotkey, "ss58_address", None)
                except Exception:
                    hotkey = None
                self._ledger(
                    "validator_meta",
                    {
                        **self._epoch_meta(),
                        "validator_hotkey": hotkey,
                        "validator_sa": validator_sa,
                        "validator_uid": getattr(self, "uid", None),
                        "netuid": getattr(config, "netuid", None) if config is not None else None,
                        "chain_endpoint": getattr(getattr(config, "subtensor", None), "chain_endpoint", None)
                        if config is not None
                        else None,
                        "validator_ip": getattr(axon, "external_ip", None) if axon is not None else None,
                        "validator_port": getattr(axon, "external_port", None) if axon is not None else None,
                    },
                )
            except Exception:
                pass

        if PRE_GENERATED_TASKS <= 0:
            bt.logging.info(f"Task pooling disabled (ALPHACORE_PRE_GENERATED_TASKS={PRE_GENERATED_TASKS}); generating tasks on-demand.")
            all_tasks: List[ACTaskSpec] = []
            try:
                self._current_round_id = round_id or str(uuid4())
                max_tries = int(os.getenv("ALPHACORE_TASKGEN_ON_DEMAND_MAX_TRIES", "20") or "20")
                max_tries = max(1, max_tries)
                retry_sleep_s = float(os.getenv("ALPHACORE_TASKGEN_ON_DEMAND_RETRY_SLEEP_S", "1.0") or "1.0")
                retry_sleep_s = max(0.0, retry_sleep_s)

                target_tasks = max(1, int(TASKS_PER_ROUND))
                for task_index in range(target_tasks):
                    last_error: Optional[Exception] = None
                    for attempt in range(1, max_tries + 1):
                        started = time.time()
                        try:
                            task_spec = await asyncio.to_thread(self._generation_pipeline.generate)
                            trace: Dict[str, Any] = {}
                            try:
                                trace = dict(
                                    getattr(self._generation_pipeline.instruction_generator, "last_trace", {}) or {}
                                )
                            except Exception:
                                trace = {}
                            invariants = self._extract_task_invariants(task_spec)
                            if not invariants:
                                raise RuntimeError(
                                    "Generated task has no invariants; refusing to dispatch/validate an unverifiable task."
                                )
                            all_tasks.append(task_spec)
                            elapsed_one = time.time() - started
                            bt.logging.info(
                                f"✓ Generated task {task_index + 1}/{target_tasks} in {elapsed_one:.2f}s "
                                f"(attempt {attempt}/{max_tries})"
                            )
                            if VERBOSE_TASK_LOGGING:
                                self._log_task_spec(task_spec, trace)
                            break
                        except Exception as exc:
                            last_error = exc
                            bt.logging.warning(
                                f"Task generation attempt {attempt}/{max_tries} failed: {exc}"
                            )
                            if attempt < max_tries and retry_sleep_s > 0:
                                await asyncio.sleep(retry_sleep_s)
                    else:
                        raise RuntimeError(
                            f"Failed to generate task {task_index + 1}/{target_tasks} after {max_tries} attempts."
                        ) from last_error

                    task_id = getattr(task_spec, "task_id", "") or ""
                    if task_id:
                        try:
                            task_json = self._extract_validation_task_json(task_spec)
                            if isinstance(task_json, dict):
                                by_round = self._validation_task_json_by_round.setdefault(
                                    str(self._current_round_id), {}
                                )
                                by_round[str(task_id)] = task_json
                        except Exception:
                            pass
                        self._task_generation_traces[task_id] = {
                            "generated_at": time.time(),
                            "generation_time_s": float(elapsed_one),
                            "prompt": getattr(task_spec, "prompt", None),
                            "prompt_trace": trace,
                            **self._epoch_meta(),
                        }

                self._current_round_tasks = all_tasks
                generation_time = time.time() - self._task_generation_start
                bt.logging.info(
                    f"✓ Generated {len(all_tasks)} tasks in {generation_time:.2f}s | "
                    f"round_id={self._current_round_id} | pool_remaining=0"
                )
                return all_tasks
            except Exception as e:
                bt.logging.error(f"✗ Task generation failed: {e}")
                raise

        # Refill pool if depleted
        await self._ensure_task_pool()

        # Pull tasks from pool
        all_tasks: List[ACTaskSpec] = []
        tasks_to_generate = min(TASKS_PER_ROUND, len(self._task_pool))

        try:
            for i in range(tasks_to_generate):
                if self._task_pool:
                    task_spec = self._task_pool.pop(0)
                    all_tasks.append(task_spec)
                    bt.logging.debug(f"Pulled task {i+1}/{tasks_to_generate} from pool: {task_spec.task_id}")

            # Use the provided round ID if one was supplied by the orchestrator.
            # This keeps dispatch/handshake/evaluation keyed to the same round.
            self._current_round_id = round_id or str(uuid4())
            self._current_round_tasks = all_tasks

            generation_time = time.time() - self._task_generation_start
            bt.logging.info(
                f"✓ Generated {len(all_tasks)} tasks in {generation_time:.2f}s | "
                f"round_id={self._current_round_id} | pool_remaining={len(self._task_pool)}"
            )

            self._ledger(
                "round_tasks_selected",
                {
                    "round_id": self._current_round_id,
                    **self._epoch_meta(),
                    "generation_time_s": float(generation_time),
                    "pool_remaining": int(len(self._task_pool)),
                    "tasks": [
                        {
                            "task_id": getattr(task_spec, "task_id", ""),
                            "provider": getattr(task_spec, "provider", ""),
                            "kind": getattr(task_spec, "kind", ""),
                            "prompt": getattr(task_spec, "prompt", None),
                            "trace": self._task_generation_traces.get(getattr(task_spec, "task_id", ""), {}),
                        }
                        for task_spec in all_tasks
                    ],
                },
            )

            # Background refill pool
            asyncio.create_task(self._ensure_task_pool())

            return all_tasks

        except Exception as e:
            bt.logging.error(f"✗ Task generation failed: {e}")
            raise

    async def _ensure_task_pool(self) -> None:
        """Ensure task pool is sufficiently filled."""
        target = max(0, int(PRE_GENERATED_TASKS))
        if target <= 0:
            return

        healthy_threshold = max(1, target // 2)
        if len(self._task_pool) >= healthy_threshold:
            return  # Pool is healthy

        start = time.time()
        to_generate = target - len(self._task_pool)

        bt.logging.info(f"🔄 Refilling task pool: {len(self._task_pool)} → {target} (+{to_generate} tasks)")

        try:
            if to_generate <= 0:
                return
            for i in range(to_generate):
                started = time.time()
                task_spec = self._generation_pipeline.generate()
                self._task_pool.append(task_spec)
                elapsed_one = time.time() - started
                epoch_meta = self._epoch_meta()

                task_id = getattr(task_spec, "task_id", "") or ""
                trace = {}
                try:
                    trace = dict(getattr(self._generation_pipeline.instruction_generator, "last_trace", {}) or {})
                except Exception:
                    trace = {}

                invariants = []
                try:
                    params = getattr(task_spec, "params", None)
                    if isinstance(params, dict):
                        task_obj = params.get("task") or {}
                        if isinstance(task_obj, dict):
                            invariants = task_obj.get("invariants") or []
                except Exception:
                    invariants = []

                trace_record = {
                    "generated_at": time.time(),
                    "generation_time_s": float(elapsed_one),
                    "prompt": getattr(task_spec, "prompt", None),
                    "invariants": invariants,
                    "prompt_trace": trace,
                    **epoch_meta,
                }
                if task_id:
                    self._task_generation_traces[task_id] = trace_record

                self._ledger(
                    "task_generated",
                    {
                        "task_id": task_id,
                        "provider": getattr(task_spec, "provider", ""),
                        "kind": getattr(task_spec, "kind", ""),
                        "generation_time_s": float(elapsed_one),
                        "prompt_trace": trace,
                        "invariants": invariants,
                        **epoch_meta,
                    },
                )
                if (i + 1) % 5 == 0:
                    bt.logging.debug(f"Pre-generated {i+1}/{to_generate} tasks")
                    await asyncio.sleep(0)  # Yield to event loop

            elapsed = time.time() - start
            self._pool_generation_time = elapsed
            per_task = (elapsed / float(to_generate)) if to_generate else 0.0
            bt.logging.info(
                f"✓ Task pool refilled: {len(self._task_pool)} tasks in {elapsed:.2f}s "
                f"(~{per_task:.2f}s per task)"
            )
        except Exception as e:
            bt.logging.error(f"✗ Task pool refill failed: {e}")

    def get_current_round_tasks(self) -> List[ACTaskSpec]:
        """Get tasks for the current round."""
        return self._current_round_tasks.copy()

    def get_current_round_id(self) -> str:
        """Get the current round ID."""
        return self._current_round_id or str(uuid4())

    def clear_round_tasks(self) -> None:
        """Clear tasks after round completion."""
        self._current_round_tasks = []
        self._current_round_id = None
