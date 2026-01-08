"""
Synapse handling utilities for AlphaCore validators.

Currently supports dispatching TaskSynapse payloads to miners and collecting
their responses.  Retries and richer error handling can be layered on once
the pipeline stabilises.
"""

from __future__ import annotations

from typing import List, Optional

import asyncio
import bittensor as bt
from bittensor import AxonInfo

from subnet.protocol import TaskSynapse


async def send_task_synapse_to_miners(
    validator,
    miner_axons: List[AxonInfo],
    task_synapse: TaskSynapse,
    timeout: int = 60,  # seconds
) -> List[Optional[TaskSynapse]]:
    """
    Broadcast a TaskSynapse to the provided miner axons.

    Args:
        validator: The validator neuron issuing the request.
        miner_axons: Target miner axon descriptors.
        task_synapse: Payload to send.
        timeout: Per-request timeout in seconds.
    """
    if not miner_axons:
        return []

    task_synapse.version = getattr(validator, "version", "alpha-core")

    if not hasattr(validator, "_dendrite_request"):
        bt.logging.warning("Validator dendrite not initialised; returning empty responses.")
        return [None for _ in miner_axons]

    try:
        responses = await validator._dendrite_request(
            axons=miner_axons,
            synapse=task_synapse,
            deserialize=True,
            timeout=timeout,
        )
    except Exception as exc:
        bt.logging.warning(f"Failed to dispatch TaskSynapse: {exc}")
        return [None for _ in miner_axons]

    # Some dendrite implementations may return a coroutine; ensure resolution.
    if asyncio.iscoroutine(responses):
        responses = await responses

    if responses is None:
        return [None for _ in miner_axons]

    return list(responses)
