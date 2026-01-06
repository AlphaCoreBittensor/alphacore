"""
Task dispatch mixin for sending tasks to miners.

Handles sending task specifications to miners via dendrite and collecting responses.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional, Tuple

import bittensor as bt

from modules.models import ACTaskSpec
from subnet.protocol import TaskSynapse
from subnet.validator.config import MINER_CONCURRENCY
from subnet.validator.config import DISPATCH_PROGRESS_LOG_INTERVAL_S
from subnet.validator.config import TASK_SYNAPSE_TIMEOUT_SECONDS
from subnet.validator.task_ledger import TaskLedger


class TaskDispatchMixin:
    """Send tasks to miners via dendrite and collect responses."""

    def __init__(self, **kwargs):
        """Initialize task dispatch state."""
        super().__init__(**kwargs)  # Pass to next in MRO
        self._dispatcher_start: Optional[float] = None
        self._task_responses: dict = {}  # round_id -> {uid: response}
        self._latency_by_round: dict = {}  # round_id -> {uid: latency_seconds}
        self._dispatch_failures_by_round: dict = {}  # round_id -> {(uid, task_id): {reason, error_type, error}}
        self._task_ledger: TaskLedger = getattr(self, "_task_ledger", TaskLedger())

    def _ledger(self, event: str, payload: Dict) -> None:
        try:
            self._task_ledger.write(event, payload)
        except Exception:
            return

    async def _run_dispatch_phase(
        self,
        tasks: List[ACTaskSpec],
        *,
        targets: Optional[List[Tuple[int, bt.AxonInfo]]] = None,
    ) -> Dict[int, Dict[str, Optional[TaskSynapse]]]:
        """
        Send tasks to miners and collect acknowledgments.

        Args:
            tasks: List of ACTaskSpec to send to miners

        Returns:
            Dictionary mapping uid -> TaskSynapse response
        """
        self._dispatcher_start = time.time()
        if targets is None:
            targets = [(uid, ax) for uid, ax in enumerate(self.metagraph.axons)]
        bt.logging.info(
            f"📤 Dispatching {len(tasks)} tasks to {len(targets)} targets "
            f"(timeout={TASK_SYNAPSE_TIMEOUT_SECONDS}s, concurrency={MINER_CONCURRENCY})"
        )
        round_id = self.get_current_round_id()
        try:
            self._ledger(
                "dispatch_start",
                {
                    "round_id": round_id,
                    "task_ids": [getattr(t, "task_id", "") for t in (tasks or [])],
                    "targets": [
                        {
                            "uid": int(uid),
                            "axon_ip": getattr(ax, "ip", None),
                            "axon_port": getattr(ax, "port", None),
                            "hotkey": getattr(ax, "hotkey", None),
                        }
                        for uid, ax in (targets or [])
                    ],
                },
            )
        except Exception:
            pass

        # Convert each ACTaskSpec to TaskSynapse
        synapses = []
        for i, task in enumerate(tasks):
            try:
                synapse = TaskSynapse.from_spec(task)
                synapses.append(synapse)
            except Exception as e:
                bt.logging.error(f"Failed to convert task {i} to synapse: {e}")
                continue

        try:
            # Send each task synapse to all miners via dendrite
            sem = asyncio.Semaphore(max(1, int(MINER_CONCURRENCY)))

            async def _send_task_to_miner(
                synapse: TaskSynapse, uid: int, ax: bt.AxonInfo
            ) -> Tuple[int, str, Optional[TaskSynapse], float, str, Optional[str]]:
                """Send one task to one miner."""
                start = time.time()
                try:
                    async with sem:
                        # Bound the await on our side as well; some underlying clients
                        # may not reliably enforce the requested timeout.
                        resp = await asyncio.wait_for(
                            self.dendrite(
                                axons=[ax],
                                synapse=synapse,
                                deserialize=False,
                                timeout=TASK_SYNAPSE_TIMEOUT_SECONDS,
                            ),
                            timeout=float(TASK_SYNAPSE_TIMEOUT_SECONDS),
                        )
                    # dendrite returns a list when axons is a list
                    resp0 = resp[0] if isinstance(resp, (list, tuple)) and resp else resp
                    # Defensive: ensure miner isn't responding with a different task_id.
                    try:
                        if resp0 is not None and hasattr(resp0, "task_id") and resp0.task_id != synapse.task_id:
                            bt.logging.warning(
                                f"✗ UID {uid} responded with mismatched task_id "
                                f"(sent={synapse.task_id}, got={getattr(resp0, 'task_id', None)}); ignoring"
                            )
                            resp0 = None
                    except Exception:
                        pass
                    latency = time.time() - start
                    return uid, synapse.task_id, resp0, latency, "ok", None
                except asyncio.TimeoutError:
                    latency = time.time() - start
                    bt.logging.debug(
                        f"✗ Timeout waiting for UID {uid} task {synapse.task_id[:8]}... | latency={latency:.2f}s"
                    )
                    return uid, synapse.task_id, None, latency, "timeout", None
                except Exception as e:
                    latency = time.time() - start
                    bt.logging.debug(f"✗ Error sending task {synapse.task_id} to UID {uid}: {e}")
                    return uid, synapse.task_id, None, latency, "error", f"{type(e).__name__}: {e}"

            # Send all tasks to all targets
            all_sends: List[asyncio.Task] = []
            for synapse in synapses:
                for uid, ax in targets:
                    all_sends.append(
                        asyncio.create_task(
                            _send_task_to_miner(synapse, uid, ax),
                            name=f"dispatch:{uid}:{synapse.task_id}",
                        )
                    )

            send_results: List[
                Tuple[int, str, Optional[TaskSynapse], float, str, Optional[str]]
            ] = []
            pending = set(all_sends)
            total_sends = len(all_sends)
            last_progress_log_at = time.time()
            progress_interval_s = max(1.0, float(DISPATCH_PROGRESS_LOG_INTERVAL_S))

            while pending:
                done, pending = await asyncio.wait(
                    pending,
                    timeout=progress_interval_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    try:
                        send_results.append(task.result())
                    except Exception as exc:
                        # Should be rare because _send_task_to_miner returns status on error,
                        # but guard so dispatch can't stall on an exception.
                        name = getattr(task, "get_name", lambda: "dispatch:unknown")()
                        bt.logging.warning(f"Dispatch task crashed ({name}): {exc}")

                now = time.time()
                if now - last_progress_log_at >= progress_interval_s:
                    bt.logging.info(
                        f"Dispatch progress {len(send_results)}/{total_sends} "
                        f"(pending={len(pending)}) | elapsed={now - self._dispatcher_start:.1f}s"
                    )
                    last_progress_log_at = now

            # Process responses: uid -> task_id -> response
            responses_dict: Dict[int, Dict[str, Optional[TaskSynapse]]] = {}
            latency_map: Dict[Tuple[int, str], float] = {}
            successful = 0

            failures: Dict[Tuple[int, str], Dict[str, Optional[str]]] = {}
            for uid, task_id, response, latency, status, error in send_results:
                responses_dict.setdefault(uid, {})[task_id] = response
                latency_map[(uid, task_id)] = float(latency)
                if response is None:
                    failures[(int(uid), str(task_id))] = {
                        "reason": str(status),
                        "error": error,
                    }

                if response is not None:
                    successful += 1
                    bt.logging.debug(
                        f"✓ Miner UID {uid} acknowledged task {task_id[:8]}... | latency={latency:.2f}s"
                    )
                else:
                    bt.logging.debug(
                        f"✗ No response from miner UID {uid} for task {task_id[:8]}... | latency={latency:.2f}s"
                    )

            dispatch_time = time.time() - self._dispatcher_start
            bt.logging.info(
                f"✓ Task dispatch completed in {dispatch_time:.2f}s | "
                f"Successful: {successful}/{total_sends}"
            )

            self._task_responses[round_id] = responses_dict
            self._latency_by_round[round_id] = latency_map
            self._dispatch_failures_by_round[round_id] = failures
            try:
                self._ledger(
                    "dispatch_complete",
                    {
                        "round_id": round_id,
                        "dispatch_time_s": float(dispatch_time),
                        "total_sends": int(total_sends),
                        "successful": int(successful),
                        "results": [
                            {
                                "uid": int(uid),
                                "task_id": str(task_id),
                                "ack": bool(response is not None),
                                "status": str(status),
                                "error": error,
                                "latency_s": float(latency),
                                "workspace_zip_sha256": getattr(response, "workspace_zip_sha256", None) if response is not None else None,
                                "workspace_zip_size_bytes": getattr(response, "workspace_zip_size_bytes", None) if response is not None else None,
                            }
                            for uid, task_id, response, latency, status, error in send_results
                        ],
                    },
                )
            except Exception:
                pass
            return responses_dict

        except Exception as e:
            bt.logging.error(f"✗ Task dispatch failed: {e}")
            raise

    def get_task_responses(self, round_id: Optional[str] = None) -> dict:
        """Get task responses for a round."""
        if round_id is None:
            round_id = self.get_current_round_id()
        return self._task_responses.get(round_id, {})

    def clear_task_responses(self, round_id: Optional[str] = None) -> None:
        """Clear task responses for a round."""
        if round_id is None:
            round_id = self.get_current_round_id()
        if round_id in self._task_responses:
            del self._task_responses[round_id]
        if round_id in self._latency_by_round:
            del self._latency_by_round[round_id]
        if round_id in self._dispatch_failures_by_round:
            del self._dispatch_failures_by_round[round_id]

    def get_dispatch_failures(self, round_id: Optional[str] = None) -> dict:
        """Get per-(uid, task_id) dispatch failures for a round."""
        if round_id is None:
            round_id = self.get_current_round_id()
        return self._dispatch_failures_by_round.get(round_id, {})

    def get_latencies(self, round_id: Optional[str] = None) -> dict:
        """Get per-miner latency seconds for a round."""
        if round_id is None:
            round_id = self.get_current_round_id()
        return self._latency_by_round.get(round_id, {})
