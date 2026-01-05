"""
Handshake mixin for validator - verify miner liveness before dispatch.

This phase verifies miners are online and ready before dispatching tasks.
Benefits:
- Skip offline miners, save bandwidth
- Get miner metadata and capabilities
- Verify communication channel works

Pattern from Autoppia - StartRoundSynapse handshake before TaskSynapse.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, Optional

import bittensor as bt

from subnet.protocol import StartRoundSynapse
from subnet.validator.config import HANDSHAKE_TIMEOUT_SECONDS, MINER_CONCURRENCY
from subnet.validator.task_ledger import TaskLedger


class HandshakeMixin:
    """Verify miner liveness via StartRoundSynapse before task dispatch."""

    def __init__(self, **kwargs):
        """Initialize handshake state."""
        super().__init__(**kwargs)
        self._handshake_results: dict = {}  # round_id -> {uid: StartRoundSynapse}
        self._active_miner_uids: dict = {}  # round_id -> list of active uids
        self._handshake_start: Optional[float] = None
        self._task_ledger: TaskLedger = getattr(self, "_task_ledger", TaskLedger())

    def _ledger(self, event: str, payload: Dict) -> None:
        try:
            self._task_ledger.write(event, payload)
        except Exception:
            return

    async def _run_handshake_phase(self, round_id: str) -> Dict[int, bool]:
        """
        Send handshake to all miners and collect liveness responses.

        Args:
            round_id: Unique round identifier

        Returns:
            Dictionary mapping uid -> is_alive (bool)

        Benefits:
        - Know which miners are truly online before sending tasks
        - Skip offline miners, reduce latency
        - Get miner version and capacity info
        """
        # Check metagraph is available
        if self.metagraph is None:
            bt.logging.warning("Metagraph is None - skipping handshake phase")
            return {}
        
        self._handshake_start = time.time()
        bt.logging.info(f"🤝 Handshake phase: Verifying miner liveness (round={round_id})")

        debug = (os.getenv("ALPHACORE_HANDSHAKE_DEBUG") or "").strip().lower() in ("1", "true", "yes", "on")
        uid_list = [int(uid) for uid in list(self.metagraph.uids)]
        if debug:
            bt.logging.debug(f"HANDSHAKE_QUERYING: {len(uid_list)} UIDs: {uid_list}")
            for uid in uid_list:
                ax = self.metagraph.axons[uid]
                bt.logging.debug(
                    f"  UID {uid}: {getattr(ax, 'ip', None)}:{getattr(ax, 'port', None)} "
                    f"(hotkey={str(getattr(ax, 'hotkey', ''))[:20]})"
                )

        synapse = StartRoundSynapse(
            round_id=round_id,
            timestamp=int(time.time()),
        )

        try:
            # Send handshake to all miners
            sem = asyncio.Semaphore(max(1, int(MINER_CONCURRENCY)))
            async def _send_handshake(uid: int, ax) -> tuple:
                start = time.time()
                try:
                    async with sem:
                        resp = await asyncio.wait_for(
                            self.dendrite(
                                axons=[ax],
                                synapse=synapse,
                                deserialize=False,
                                timeout=HANDSHAKE_TIMEOUT_SECONDS,
                            ),
                            timeout=float(HANDSHAKE_TIMEOUT_SECONDS) + 5.0,
                        )
                    resp_single = resp[0] if isinstance(resp, (list, tuple)) and resp else resp
                    latency = time.time() - start
                    
                    is_alive = (
                        resp_single is not None 
                        and hasattr(resp_single, "is_ready")
                        and resp_single.is_ready
                    )
                    
                    if is_alive:
                        bt.logging.debug(
                            f"✓ UID {uid} ALIVE | v{resp_single.miner_version} | "
                            f"capacity={resp_single.available_capacity} | latency={latency:.2f}s"
                        )
                    else:
                        error_msg = getattr(resp_single, "error_message", "Unknown error")
                        bt.logging.debug(
                            f"✗ UID {uid} NOT READY | Error: {error_msg} | latency={latency:.2f}s"
                        )
                    
                    return uid, resp_single, is_alive, latency
                    
                except asyncio.TimeoutError:
                    latency = time.time() - start
                    bt.logging.debug(f"✗ UID {uid} TIMEOUT | latency={latency:.2f}s")
                    return uid, None, False, latency
                except Exception as e:
                    latency = time.time() - start
                    bt.logging.debug(f"✗ UID {uid} ERROR | {e} | latency={latency:.2f}s")
                    return uid, None, False, latency

            # Gather all handshakes - only send to miners that have registered axons
            # Filter to UIDs with non-zero IP (skip validators which don't have axons)
            miner_uids: list[int] = []
            for uid in list(self.metagraph.uids):
                uid_int = int(uid)
                ax = self.metagraph.axons[uid_int]
                if getattr(ax, "ip", None) not in [None, "0.0.0.0", ""]:
                    miner_uids.append(uid_int)

            bt.logging.info(
                f"Handshake querying {len(miner_uids)}/{len(uid_list)} miners "
                f"(timeout={HANDSHAKE_TIMEOUT_SECONDS}s, concurrency={MINER_CONCURRENCY})"
            )
            if debug:
                bt.logging.debug(f"MINER_UIDS_FILTERED: {miner_uids}")
            
            tasks = [
                _send_handshake(uid, self.metagraph.axons[uid])
                for uid in miner_uids
            ]
            pending = [asyncio.create_task(coro) for coro in tasks]
            results = []
            completed = 0
            total = len(pending)
            for fut in asyncio.as_completed(pending):
                res = await fut
                results.append(res)
                completed += 1
                if completed == 1 or completed == total or completed % 5 == 0:
                    bt.logging.info(f"Handshake progress {completed}/{total}")
            if debug:
                bt.logging.debug(f"HANDSHAKE_RESULTS_COUNT: {len(results)}")

            # Process results
            alive_uids = []
            responses = {}
            alive_count = 0

            for uid, response, is_alive, latency in results:
                responses[uid] = response
                if is_alive:
                    alive_uids.append(uid)
                    alive_count += 1

            # Store results
            self._handshake_results[round_id] = responses
            self._active_miner_uids[round_id] = alive_uids

            handshake_time = time.time() - self._handshake_start
            bt.logging.info(
                f"✓ Handshake complete: {alive_count}/{len(self.metagraph.uids)} miners ALIVE | "
                f"Time: {handshake_time:.2f}s"
            )
            try:
                self._ledger(
                    "handshake_complete",
                    {
                        "round_id": round_id,
                        "handshake_time_s": float(handshake_time),
                        "alive_uids": [int(uid) for uid in alive_uids],
                        "queried_uids": [int(uid) for uid in miner_uids],
                        "miners": [
                            {
                                "uid": int(uid),
                                "axon_ip": getattr(self.metagraph.axons[uid], "ip", None),
                                "axon_port": getattr(self.metagraph.axons[uid], "port", None),
                                "hotkey": getattr(self.metagraph.axons[uid], "hotkey", None),
                                "is_alive": bool(uid in alive_uids),
                                "miner_version": getattr(responses.get(uid), "miner_version", None),
                                "available_capacity": getattr(responses.get(uid), "available_capacity", None),
                                "error_message": getattr(responses.get(uid), "error_message", None),
                            }
                            for uid in miner_uids
                        ],
                    },
                )
            except Exception:
                pass

            # Return dict of alive status for each uid
            return {int(uid): int(uid) in alive_uids for uid in list(self.metagraph.uids)}

        except Exception as e:
            bt.logging.error(f"✗ Handshake phase failed: {e}", exc_info=True)
            raise

    def get_active_miner_uids(self, round_id: Optional[str] = None) -> list:
        """Get list of UIDs that responded to handshake with is_ready=True."""
        if round_id is None:
            round_id = self.get_current_round_id()
        return self._active_miner_uids.get(round_id, [])

    def get_handshake_responses(self, round_id: Optional[str] = None) -> dict:
        """Get all handshake responses for a round."""
        if round_id is None:
            round_id = self.get_current_round_id()
        return self._handshake_results.get(round_id, {})

    def clear_handshake_state(self, round_id: Optional[str] = None) -> None:
        """Clear handshake state for a round."""
        if round_id is None:
            round_id = self.get_current_round_id()
        if round_id in self._handshake_results:
            del self._handshake_results[round_id]
        if round_id in self._active_miner_uids:
            del self._active_miner_uids[round_id]
