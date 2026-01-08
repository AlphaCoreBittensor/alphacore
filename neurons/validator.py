"""
AlphaCore validator with hybrid architecture combining simplicity and durability.

Implements the complete validator lifecycle with improved patterns from Autoppia:
1. PREPARATION: Initialize round, start block
2. GENERATION: Generate tasks for all miners
3. HANDSHAKE: Verify miner liveness before dispatch (Autoppia pattern)
4. DISPATCH: Send tasks only to active miners with retry logic
5. EXECUTION: Wait for miner responses
6. EVALUATION: Compute scores and save progress
7. FEEDBACK: Send per-task feedback for real-time learning (Autoppia pattern)
8. FINALIZATION: Set weights based on scores

"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from subnet.validator.round_manager import RoundManager

import bittensor as bt
import numpy as np

from subnet.base.validator import BaseValidatorNeuron
from subnet.bittensor_config import config as build_config
from subnet.validator.config import (
	ROUND_CADENCE_SECONDS,
	ROUND_SIZE_EPOCHS,
	SAFETY_BUFFER_EPOCHS,
	SKIP_ROUND_IF_STARTED_AFTER_FRACTION,
	WEIGHTS_EMIT_POLL_SECONDS,
	WEIGHTS_MIN_TASKS_BEFORE_EMIT,
	WEIGHTS_EMIT_BLOCK_OFFSET,
	VALIDATOR_VERSION,
)
from subnet.validator.dispatch.mixin import TaskDispatchMixin
from subnet.validator.evaluation.mixin import TaskEvaluationMixin
from subnet.validator.generation.mixin import TaskGenerationMixin
from subnet.validator.handshake.mixin import HandshakeMixin
from subnet.validator.feedback.mixin import FeedbackMixin
from subnet.validator.settlement.mixin import SettlementMixin
from subnet.protocol import TaskCleanupSynapse
from subnet.validator.config import VALIDATION_API_ENABLED, VALIDATION_API_ENDPOINT
# Bittensor tempo (blocks per epoch)
# 360 blocks * 12 seconds per block = 4320 seconds = 72 minutes per epoch
DEFAULT_TEMPO = 360


class Validator(
	TaskGenerationMixin,
	TaskDispatchMixin,
	TaskEvaluationMixin,
	HandshakeMixin,
	FeedbackMixin,
	SettlementMixin,
	BaseValidatorNeuron,
):
	"""
	AlphaCore validator with hybrid architecture.

	Implements the complete validator lifecycle with durability and resilience:
	1. GENERATION: Generate tasks for all miners
	2. HANDSHAKE: Verify miner liveness (skip offline miners)
	3. DISPATCH: Send tasks to active miners with retry logic
	4. EXECUTION: Wait for miner responses
	5. EVALUATION: Compute scores incrementally
	6. FEEDBACK: Send per-task scores for real-time learning
	7. FINALIZATION: Set weights based on scores

	Hybrid approach combines:
	- AlphaCore: Batch parallel dispatch, simple logic
	- Autoppia: Handshake, feedback for durability
	"""

	neuron_type: str = "AlphaCoreValidator"

	def __init__(self, config: Optional[bt.Config] = None) -> None:
		"""Initialize validator with all mixins."""
		# Initialize BaseValidator (which covers all mixins via MRO)
		try:
			super().__init__(config=config or build_config(role="validator"))
		except Exception as e:
			bt.logging.error(f"Error in super().__init__(): {e}", exc_info=True)
			raise

		# Ensure wallet identity is configured (name + hotkey) before registration
		try:
			wallet_name = getattr(self.config.wallet, "name", None)
			wallet_hotkey = getattr(self.config.wallet, "hotkey", None)
			if not wallet_name or not wallet_hotkey:
				bt.logging.error(
					"Validator wallet not configured. Set ALPHACORE_WALLET_NAME and ALPHACORE_WALLET_HOTKEY "
					"(or pass --wallet.name and --wallet.hotkey)."
				)
				raise RuntimeError("Missing wallet.name or wallet.hotkey")
		except Exception:
			raise

		# This validator implementation is not concurrency-safe across multiple `forward()` calls
		# (it shares round/task state across mixins). Enforce a single concurrent forward.
		try:
			num_forwards = int(getattr(self.config.neuron, "num_concurrent_forwards", 1) or 1)
		except Exception:
			num_forwards = 1
		if num_forwards != 1:
			bt.logging.warning(
				f"Overriding neuron.num_concurrent_forwards={num_forwards} to 1 (validator forward loop is not concurrency-safe)."
			)
			try:
				self.config.neuron.num_concurrent_forwards = 1
			except Exception:
				pass

		# Initialize round management with epoch-based timing
		bt.logging.info(f"Validator init: subtensor={getattr(self, 'subtensor', 'NOT SET')}")

		# Get tempo - it's a method that needs netuid parameter
		tempo = DEFAULT_TEMPO
		if self.subtensor and hasattr(self.subtensor, "tempo") and callable(self.subtensor.tempo):
			try:
				tempo = int(
					self._with_subtensor(self.subtensor.tempo, netuid=self.config.netuid)
				)
			except Exception as exc:
				bt.logging.debug(f"Tempo lookup failed: {exc}")
				tempo = DEFAULT_TEMPO

		round_duration_blocks = max(1, int(ROUND_SIZE_EPOCHS * tempo - SAFETY_BUFFER_EPOCHS * tempo))
		self.tempo = tempo
		self.round_duration_blocks = round_duration_blocks
		self.round_manager = RoundManager(round_duration_blocks=round_duration_blocks, tempo=tempo)

		# Configuration
		self.round_cadence = ROUND_CADENCE_SECONDS
		self.skip_round_if_started_after_fraction = SKIP_ROUND_IF_STARTED_AFTER_FRACTION
		self.version = "alpha-core.v2-hybrid"

		self._weights_emit_poll_seconds = max(0.5, float(WEIGHTS_EMIT_POLL_SECONDS))
		self._weights_emit_block_offset = int(WEIGHTS_EMIT_BLOCK_OFFSET)
		self._last_weights_emit_epoch: Optional[int] = None
		self._weights_emitter_task: Optional[asyncio.Task] = None
		min_tasks_override = None
		try:
			min_tasks_override = getattr(getattr(self.config, "validator", None), "weights_min_tasks_before_emit", None)
		except Exception:
			min_tasks_override = None
		if min_tasks_override is None:
			min_tasks_override = WEIGHTS_MIN_TASKS_BEFORE_EMIT
		self._weights_min_tasks_before_emit = max(0, int(min_tasks_override))
		self._weights_tasks_completed = 0
		self._weights_emitter_logged_boot = False
		self._weights_emitter_last_epoch_logged: Optional[int] = None
		self._timed_heartbeat_interval_seconds = self._env_float(
			"ALPHACORE_TIMED_HEARTBEAT_SECONDS",
			default=60.0,
		)
		self._timed_heartbeat_interval_seconds = max(5.0, float(self._timed_heartbeat_interval_seconds))
		self._timed_heartbeat_task: Optional[asyncio.Task] = None
		self._next_round_at: Optional[float] = None

		bt.logging.info(f"✓ Initialized {self.neuron_type} v{self.version}")
		bt.logging.info("  - Handshake enabled (skip offline miners)")
		bt.logging.info("  - Feedback enabled (real-time learning)")

		# Local test mode: a deterministic local harness that exercises the same
		# validator↔miner plumbing used on mainnet, but can use fixture tasks/zips.
		self._local_test_enabled = self._env_flag("ALPHACORE_LOCAL_TEST_MODE")
		self._local_test_ran = False
		self._local_test_lock = asyncio.Lock()
		self._local_test_interval_seconds = self._env_float("ALPHACORE_LOCAL_TEST_INTERVAL_SECONDS", default=5.0)
		self._local_test_interval_seconds = max(0.25, float(self._local_test_interval_seconds))
		self._local_test_use_taskgen_prompt = self._env_flag("ALPHACORE_LOCAL_TEST_USE_TASKGEN_PROMPT")
		self._local_test_prompt_pipeline = None
		self._metagraph_resync_seconds = self._env_float(
			"ALPHACORE_METAGRAPH_RESYNC_SECONDS",
			default=(30.0 if self._local_test_enabled else 60.0),
		)
		self._metagraph_resync_seconds = max(1.0, float(self._metagraph_resync_seconds))

		# Fail-fast if validation is expected but the Validation API is not healthy.
		# This prevents silently running rounds with all-0 scores because validation fails closed.
		try:
			if VALIDATION_API_ENABLED and self._env_flag("ALPHACORE_FAIL_FAST_ON_VALIDATION_API", default=True):
				self._require_validation_api_healthy()
		except Exception as exc:
			bt.logging.error(f"[startup] Validation API not ready: {exc}")
			raise

		# Round activity lockfile: written at round start and removed at round end.
		# Used by host auto-updaters to avoid restarting the validator mid-round.
		self._round_lockfile_path = Path(
			os.getenv("ALPHACORE_ROUND_LOCKFILE", "/tmp/alphacore-validator-round.lock")
		).expanduser()
		try:
			# A validator restart is, by definition, not mid-round. Clear any stale lockfile
			# so auto-updaters don't wedge forever after a crash/restart.
			if self._round_lockfile_path.exists():
				self._round_lockfile_path.unlink()
		except Exception:
			pass

	def _require_validation_api_healthy(self) -> None:
		endpoint = str(VALIDATION_API_ENDPOINT or "").rstrip("/")
		if not endpoint:
			raise RuntimeError("ALPHACORE_VALIDATION_API_ENDPOINT is empty")
		url = f"{endpoint}/health"
		timeout_s = float(self._env_float("ALPHACORE_VALIDATION_API_STARTUP_TIMEOUT_S", default=2.0))
		require_token = self._env_flag("ALPHACORE_VALIDATION_REQUIRE_TOKEN", default=True)
		require_sandbox = self._env_flag("ALPHACORE_VALIDATION_REQUIRE_SANDBOX", default=False)

		try:
			with urllib.request.urlopen(url, timeout=timeout_s) as resp:
				body = resp.read().decode("utf-8", errors="replace")
		except Exception as exc:
			raise RuntimeError(f"Health check failed for {url}: {exc}") from exc

		try:
			data = json.loads(body)
		except Exception as exc:
			raise RuntimeError(f"Health check returned non-JSON from {url}: {body[:200]}") from exc

		status = str(data.get("status", ""))
		token_ready = bool(data.get("token_ready", False))
		token_error = data.get("token_error")
		sandbox_ready = bool(data.get("sandbox_ready", False))

		status_norm = status.strip().lower()
		if status_norm not in ("ok", "healthy"):
			raise RuntimeError(f"Validation API unhealthy: status={status}")
		if require_token and not token_ready:
			raise RuntimeError(f"Validation API token not ready: {token_error}")
		if require_sandbox and not sandbox_ready:
			raise RuntimeError("Validation API sandbox not ready")
		self._last_metagraph_sync_at = 0.0

		self._loop_mode = (os.getenv("ALPHACORE_LOOP_MODE") or ("timed" if self._local_test_enabled else "epoch")).strip().lower()
		self._tick_seconds = self._env_float("ALPHACORE_TICK_SECONDS", default=self._local_test_interval_seconds)
		self._tick_seconds = max(0.25, float(self._tick_seconds))

		# Epoch/round gating observability (useful when running in epoch-aware mode).
		self._epoch_log_enabled = self._env_flag(
			"ALPHACORE_EPOCH_LOGGING",
			default=True,
		)
		self._epoch_log_interval_s = self._env_float(
			"ALPHACORE_EPOCH_LOG_INTERVAL_S",
			default=(60.0 if self._epoch_log_enabled else 0.0),
		)
		self._epoch_log_interval_s = max(0.0, float(self._epoch_log_interval_s))
		self._last_epoch_log_at = 0.0
		self._last_epoch_logged = None
		# In epoch-aware mode, start at most one round per epoch by default.
		self._one_round_per_epoch = self._env_flag("ALPHACORE_ONE_ROUND_PER_EPOCH", default=True)
		self._last_round_epoch_started = None
		# Optional: stagger round starts across the epoch in N slots so multiple
		# validators do not all dispatch at the same time.
		config_epoch_slots = self._config_int("validator.epoch_slots")
		if config_epoch_slots is not None:
			self._epoch_slots = max(1, int(config_epoch_slots))
		else:
			self._epoch_slots = max(1, int(self._env_int("ALPHACORE_EPOCH_SLOTS", default=1)))
		try:
			if self._loop_mode == "timed":
				bt.logging.info(
					f"⏱️ Timed mode enabled: tick={float(self._tick_seconds):.1f}s"
				)
			elif self._epoch_slots > 1:
				slot_index = self._epoch_slot_index(self._epoch_slots)
				window_start = float(slot_index) / float(self._epoch_slots)
				window_end = float(slot_index + 1) / float(self._epoch_slots)
				bt.logging.info(
					f"🪟 Epoch slotting enabled: slots={self._epoch_slots} slot={slot_index} "
					f"window={window_start:.2f}-{window_end:.2f}"
				)
		except Exception:
			pass

	def _load_local_miner_targets(self) -> List[tuple[int, bt.AxonInfo]]:
		axons = self._load_local_miner_axons()
		# Use stable negative UIDs for local-only targets.
		return [(-(idx + 1), ax) for idx, ax in enumerate(axons)]

	def _local_miners_fallback_enabled(self) -> bool:
		return self._env_flag("ALPHACORE_LOCAL_MINERS_FALLBACK", default=True)

	def _env_float(self, key: str, default: float) -> float:
		raw = os.getenv(key)
		if raw is None or not str(raw).strip():
			return default
		try:
			return float(str(raw).strip())
		except Exception:
			return default

	def _env_int(self, key: str, default: int) -> int:
		raw = os.getenv(key)
		if raw is None or not str(raw).strip():
			return default
		try:
			return int(str(raw).strip())
		except Exception:
			return default

	def _env_flag(self, key: str, default: bool = False) -> bool:
		raw = os.getenv(key)
		if raw is None:
			return default
		return raw.strip().lower() in ("1", "true", "yes", "y", "on")

	def _config_int(self, key: str) -> Optional[int]:
		node = getattr(self, "config", None)
		for part in key.split("."):
			node = getattr(node, part, None)
			if node is None:
				return None
		try:
			return int(node)
		except Exception:
			return None

	def _epoch_slot_index(self, slot_count: int) -> int:
		slot_count = max(1, int(slot_count))
		config_override = self._config_int("validator.epoch_slot_index")
		if config_override is not None:
			try:
				return int(config_override) % slot_count
			except Exception:
				pass
		override = os.getenv("ALPHACORE_EPOCH_SLOT_INDEX")
		if override is not None and str(override).strip():
			try:
				return int(str(override).strip()) % slot_count
			except Exception:
				pass

		uid = getattr(self, "uid", None)
		try:
			if uid is not None:
				return int(uid) % slot_count
		except Exception:
			pass

		hotkey = None
		try:
			hotkey_obj = getattr(getattr(self, "wallet", None), "hotkey", None)
			hotkey = getattr(hotkey_obj, "ss58_address", None)
		except Exception:
			hotkey = None
		seed = str(hotkey or "")
		if not seed:
			return 0

		digest = hashlib.sha256(seed.encode("utf-8", errors="ignore")).digest()
		value = int.from_bytes(digest[:4], "big", signed=False)
		return value % slot_count

	def _maybe_log_epoch_gate(self, *, current_block: int, started: bool, reason: str) -> None:
		if not getattr(self, "_epoch_log_enabled", False):
			return
		try:
			now = time.time()
			interval = float(getattr(self, "_epoch_log_interval_s", 0.0) or 0.0)
			tempo = int(getattr(self.round_manager, "tempo", DEFAULT_TEMPO) or DEFAULT_TEMPO)
			tempo = max(1, tempo)
			epoch = int(current_block) // tempo
			blocks_into_epoch = int(current_block) - (epoch * tempo)
			blocks_until_epoch_end = max(0, tempo - blocks_into_epoch)

			last_at = float(getattr(self, "_last_epoch_log_at", 0.0) or 0.0)
			last_epoch = getattr(self, "_last_epoch_logged", None)
			if interval > 0 and (now - last_at) < interval and last_epoch == epoch:
				return
			self._last_epoch_log_at = now
			self._last_epoch_logged = epoch

			round_blocks = int(getattr(self, "round_duration_blocks", 0) or 0)
			progress = float(blocks_into_epoch) / float(tempo)
			bt.logging.info(
				f"⏳ Epoch gate: block={int(current_block)} epoch={int(epoch)} tempo={int(tempo)} "
				f"progress={progress:.3f} blocks_into_epoch={blocks_into_epoch}/{tempo} "
				f"until_epoch_end={blocks_until_epoch_end} round_blocks={round_blocks} "
				f"start_round={bool(started)} reason={reason}"
			)
		except Exception:
			return

	def _load_local_miner_axons(self) -> List[bt.AxonInfo]:
		"""
		Local-dev fallback axon discovery for local test mode.

		When miners are not registered (or fail to serve their axon to chain),
		the metagraph will show `0.0.0.0:0` endpoints. For local tests, we can
		directly target `127.0.0.1:<port>` and use the miner wallet files to
		derive the miner hotkey ss58 (required for bittensor signature checks).
		"""
		raw = (os.getenv("ALPHACORE_LOCAL_MINERS_JSON") or "").strip()
		if not raw:
			return []

		try:
			entries = json.loads(raw)
		except Exception as exc:
			bt.logging.warning(f"Invalid ALPHACORE_LOCAL_MINERS_JSON: {exc}")
			return []

		if not isinstance(entries, list):
			bt.logging.warning("ALPHACORE_LOCAL_MINERS_JSON must be a JSON list")
			return []

		axons: List[bt.AxonInfo] = []
		for idx, entry in enumerate(entries):
			if not isinstance(entry, dict):
				continue

			ip = str(entry.get("ip") or "127.0.0.1")
			try:
				port = int(entry.get("port"))
			except Exception:
				bt.logging.warning(f"Local miner entry {idx} missing valid port")
				continue

			hotkey_ss58 = entry.get("hotkey_ss58")
			coldkey_ss58 = entry.get("coldkey_ss58")

			if not hotkey_ss58:
				wallet_name = entry.get("wallet_name") or entry.get("wallet") or entry.get("name")
				hotkey_name = entry.get("hotkey_name") or entry.get("hotkey") or wallet_name
				wallet_path = entry.get("wallet_path") or getattr(self.config.wallet, "path", None)
				try:
					wallet = bt.Wallet(name=wallet_name, hotkey=hotkey_name, path=wallet_path)
					hotkey_ss58 = wallet.hotkey.ss58_address
					if not coldkey_ss58:
						coldkeypub = getattr(wallet, "coldkeypub", None)
						if coldkeypub is not None:
							coldkey_ss58 = coldkeypub.ss58_address
				except Exception as exc:
					bt.logging.warning(f"Unable to load local miner wallet for {wallet_name}/{hotkey_name}: {exc}")
					continue

			if not coldkey_ss58:
				coldkey_ss58 = hotkey_ss58

			axons.append(
				bt.AxonInfo(
					version=0,
					ip=ip,
					port=port,
					ip_type=4,
					hotkey=str(hotkey_ss58),
					coldkey=str(coldkey_ss58),
				)
			)

		return axons

	async def _run_local_test_cycle(self) -> None:
		"""Run a single validator→miner→validator local test cycle."""
		from modules.models import ACTaskSpec, ACPolicy, VerifyPlan
		from subnet.protocol import StartRoundSynapse, TaskSynapse, TaskCleanupSynapse

		axons = self._load_local_miner_axons()
		if not axons:
			bt.logging.warning(
				"Local test enabled, but no local miners found. "
				"Set ALPHACORE_LOCAL_MINERS_JSON or register/serve miners to chain."
			)
			return

		round_id = f"local-test-{int(time.time())}"
		bt.logging.info(f"🧪 Local test: sending StartRoundSynapse to {len(axons)} miner(s)")

		handshake = StartRoundSynapse(
			round_id=round_id,
			timestamp=int(time.time()),
			validator_version=VALIDATOR_VERSION,
		)
		handshake_resps = await self._dendrite_request(
			axons=axons,
			synapse=handshake,
			deserialize=False,
			timeout=10,
		)

		ready = 0
		for resp in handshake_resps or []:
			if resp is not None and getattr(resp, "is_ready", False):
				ready += 1
		bt.logging.info(f"🧪 Local test: handshake ready={ready}/{len(axons)}")

		task_id = f"local-test-{int(time.time())}"
		spec = ACTaskSpec(
			task_id=task_id,
			provider="local",
			kind="local_test",
			params={},
			policy=ACPolicy(description="local test", max_cost="low", constraints={}),
			verify_plan=VerifyPlan(kind="noop", steps=[]),
			prompt="Local test: return a small zip artifact",
			verify_fn=None,
		)

		bt.logging.info(f"🧪 Local test: sending TaskSynapse task_id={task_id}")
		task_syn = TaskSynapse.from_spec(spec)
		task_resps = await self._dendrite_request(
			axons=axons,
			synapse=task_syn,
			deserialize=False,
			timeout=30,
		)

		got_zip = 0
		for resp in task_resps or []:
			if resp is not None and getattr(resp, "workspace_zip_b64", None):
				got_zip += 1
		bt.logging.info(f"🧪 Local test: task responses with zip={got_zip}/{len(axons)}")

		validation_response = {
			"job_id": "local-test",
			"task_id": task_id,
			"result": {"status": "pass", "score": 1.0, "msg": "local test"},
			"log_url": "",
			"log_path": "",
			"submission_path": "",
			"tap": None,
		}
		validation_payload = dict(validation_response) if isinstance(validation_response, dict) else validation_response
		if isinstance(validation_payload, dict):
			validation_payload.pop("tap", None)
		cleanup = TaskCleanupSynapse(task_id=task_id, validation_response=validation_payload)

		bt.logging.info(f"🧪 Local test: sending TaskCleanupSynapse task_id={task_id}")
		cleanup_resps = await self._dendrite_request(
			axons=axons,
			synapse=cleanup,
			deserialize=False,
			timeout=10,
		)

		acked = 0
		for resp in cleanup_resps or []:
			if resp is not None and getattr(resp, "acknowledged", False):
				acked += 1
		bt.logging.info(f"🧪 Local test: cleanup acked={acked}/{len(axons)}")

	# Simple helper to keep awaiting sleep calls in one place
	async def sleep(self, seconds: float) -> None:
		await asyncio.sleep(seconds)

	def _in_weights_window(self, current_block: int) -> tuple[bool, int, float]:
		tempo = int(getattr(self.round_manager, "tempo", 0) or 0)
		if tempo <= 0:
			return False, 0, 0.0
		epoch = int(current_block // tempo)
		blocks_into = int(current_block - (epoch * tempo))
		fraction = float(blocks_into) / float(tempo)
		offset = int(self._weights_emit_block_offset)
		return (blocks_into >= offset), epoch, fraction

	def _maybe_emit_weights(self, current_block: int) -> None:
		in_window, epoch, fraction = self._in_weights_window(current_block)
		if not in_window:
			bt.logging.info(
				f"[WEIGHTS] Check: block={int(current_block)} epoch={int(epoch)} "
				f"block_in_epoch={int(current_block - epoch * self.round_manager.tempo)} "
				f"offset={int(self._weights_emit_block_offset)} action=skip_out_of_window"
			)
			return
		if self._last_weights_emit_epoch == epoch:
			bt.logging.info(
				f"[WEIGHTS] Check: block={int(current_block)} epoch={int(epoch)} "
				f"block_in_epoch={int(current_block - epoch * self.round_manager.tempo)} "
				f"offset={int(self._weights_emit_block_offset)} action=skip_already_emitted"
			)
			return
		if self._weights_tasks_completed < self._weights_min_tasks_before_emit:
			bt.logging.debug(
				f"[WEIGHTS] Skipping emission (tasks={self._weights_tasks_completed}/{self._weights_min_tasks_before_emit})"
			)
			return
		try:
			if float(np.sum(self.scores)) <= 0.0:
				bt.logging.debug(
					f"[WEIGHTS] Skipping emission in window (epoch={epoch}, frac={fraction:.3f}): no non-zero scores"
				)
				return
		except Exception:
			return
		try:
			self._emit_top_k_weights()
			bt.logging.info(
				f"[WEIGHTS] Check: block={int(current_block)} epoch={int(epoch)} "
				f"block_in_epoch={int(current_block - epoch * self.round_manager.tempo)} "
				f"offset={int(self._weights_emit_block_offset)} action=emitted"
			)
			self._last_weights_emit_epoch = epoch
			bt.logging.info(
				f"✅ [WEIGHTS] Emitted weights on chain (epoch={epoch}, block_in_epoch={int(current_block - epoch * self.round_manager.tempo)})"
			)
		except Exception as exc:
			bt.logging.error(f"✗ [WEIGHTS] Emission failed: {exc}")

	def _emit_top_k_weights(self, k: int = 5) -> None:
		from subnet.validator.config import BURN_AMOUNT_PERCENTAGE, BURN_UID

		scores = np.asarray(self.scores, dtype=np.float32)
		if scores.size == 0:
			raise RuntimeError("no scores available for weight emission")
		# Exclude burn uid from candidates
		candidates = [i for i in range(scores.size) if i != int(BURN_UID)]
		if not candidates:
			raise RuntimeError("no eligible candidates for weight emission")
		candidate_scores = scores[candidates]
		if float(np.max(candidate_scores)) <= 0.0:
			raise RuntimeError("all candidate scores are zero")

		k = max(1, min(int(k), len(candidates)))
		top_idx = np.argsort(candidate_scores)[-k:]
		top_uids = [candidates[int(i)] for i in top_idx]

		weights = np.zeros_like(scores, dtype=np.float32)
		burn_pct = float(BURN_AMOUNT_PERCENTAGE)
		burn_pct = max(0.0, min(1.0, burn_pct))
		remaining = 1.0 - burn_pct
		per_uid = remaining / float(len(top_uids))
		for uid in top_uids:
			weights[int(uid)] = per_uid
		if 0 <= int(BURN_UID) < weights.size and burn_pct > 0.0:
			weights[int(BURN_UID)] = burn_pct

		try:
			allocations = []
			for uid, weight in enumerate(weights):
				if weight > 0:
					label = "BURN" if int(uid) == int(BURN_UID) else "UID"
					allocations.append((float(weight), f"{label} {uid}={weight * 100:.2f}%"))
			allocations.sort(reverse=True)
			alloc_str = ", ".join(item for _, item in allocations)
			bt.logging.info(f"✅ [WEIGHTS] Allocation: {alloc_str}")
		except Exception:
			pass

		ema_scores = self.scores.copy()
		try:
			self.set_weights(weights)
		finally:
			self.scores = ema_scores

	async def _weights_emitter_loop(self) -> None:
		while not getattr(self, "should_exit", False):
			try:
				current_block = int(self.block)
				tempo = int(getattr(self.round_manager, "tempo", 0) or 0)
				if not self._weights_emitter_logged_boot:
					bt.logging.info(
						f"[WEIGHTS] Emitter started (offset={int(self._weights_emit_block_offset)}, "
						f"min_tasks={int(self._weights_min_tasks_before_emit)}, "
						f"poll={float(self._weights_emit_poll_seconds):.1f}s)"
					)
					self._weights_emitter_logged_boot = True
				if tempo > 0:
					epoch = int(current_block // tempo)
					if self._weights_emitter_last_epoch_logged != epoch:
						block_in_epoch = int(current_block - (epoch * tempo))
						bt.logging.info(
							f"[WEIGHTS] Epoch check: epoch={int(epoch)} "
							f"block_in_epoch={int(block_in_epoch)} "
							f"offset={int(self._weights_emit_block_offset)} "
							f"tasks={int(self._weights_tasks_completed)}/{int(self._weights_min_tasks_before_emit)}"
						)
						self._weights_emitter_last_epoch_logged = epoch
				self._maybe_emit_weights(current_block)
			except Exception as exc:
				bt.logging.debug(f"[WEIGHTS] Emission loop error: {exc}")
			await asyncio.sleep(self._weights_emit_poll_seconds)

	def _start_weights_emitter(self) -> None:
		if self._weights_emitter_task is not None and not self._weights_emitter_task.done():
			return
		try:
			self._weights_emitter_task = asyncio.create_task(self._weights_emitter_loop())
		except RuntimeError:
			# Event loop not ready yet; will retry on next forward tick.
			self._weights_emitter_task = None

	async def _timed_heartbeat_loop(self) -> None:
		while not getattr(self, "should_exit", False):
			try:
				current_block = int(self.block)
				tempo = int(getattr(self.round_manager, "tempo", 0) or 0)
				next_in = None
				if self._next_round_at is not None:
					next_in = max(0.0, float(self._next_round_at - time.time()))
				if tempo > 0:
					epoch = int(current_block // tempo)
					blocks_into = int(current_block - (epoch * tempo))
					until_end = int(tempo - blocks_into)
					bt.logging.info(
						f"⏱️ Timed mode heartbeat: block={current_block} epoch={epoch} "
						f"blocks_into_epoch={blocks_into} until_epoch_end={until_end} "
						f"next_round_in={(next_in if next_in is not None else float(self._tick_seconds)):.1f}s"
					)
				else:
					bt.logging.info(
						f"⏱️ Timed mode heartbeat: block={current_block} "
						f"next_round_in={(next_in if next_in is not None else float(self._tick_seconds)):.1f}s"
					)
			except Exception:
				pass
			await asyncio.sleep(self._timed_heartbeat_interval_seconds)

	def _start_timed_heartbeat(self) -> None:
		if self._timed_heartbeat_task is not None and not self._timed_heartbeat_task.done():
			return
		try:
			self._timed_heartbeat_task = asyncio.create_task(self._timed_heartbeat_loop())
		except RuntimeError:
			self._timed_heartbeat_task = None

	# ================================================================== #
	# MAIN VALIDATOR LOOP - HYBRID APPROACH
	# ================================================================== #

	async def forward(self) -> None:
		"""
		Execute one complete hybrid validator round with durability.

		This is called by the base validator's run loop (concurrent_forward).
		Each call represents one validation cycle with all phases:
		1. GENERATION: Create tasks with pre-generation pool
		2. HANDSHAKE: Verify miner liveness (skip offline miners)
		3. DISPATCH: Send tasks to active miners only
		4. EXECUTION: Wait for responses
		5. EVALUATION: Score miners incrementally
		6. FEEDBACK: Send per-task feedback (real-time learning)
		7. FINALIZATION: Set weights based on scores

		"""
		round_started = False
		round_completed = False
		round_id: Optional[str] = None
		tasks: List[Any] = []
		active_uids: List[int] = []
		responses: Dict[int, Any] = {}
		scores: Dict[int, float] = {}

		try:
			self._start_weights_emitter()
			if self._loop_mode == "timed":
				self._start_timed_heartbeat()
			# Timed mode: run cycles on wall-clock cadence (local development).
			if self._loop_mode == "timed":
				# Timed rounds (local-dev). If local test mode is enabled, the same pipeline
				# uses a fixed task spec + local targets instead of epoch gating.
				async with self._local_test_lock:
					self._local_test_ran = True
					await self._run_round_cycle(ignore_epoch_gating=True)
				self._next_round_at = time.time() + float(self._tick_seconds)
				try:
					current_block = int(self.block)
					tempo = int(getattr(self.round_manager, "tempo", 0) or 0)
					if tempo > 0:
						epoch = int(current_block // tempo)
						blocks_into = int(current_block - (epoch * tempo))
						until_end = int(tempo - blocks_into)
						bt.logging.info(
							f"⏱️ Timed mode heartbeat: block={current_block} epoch={epoch} "
							f"blocks_into_epoch={blocks_into} until_epoch_end={until_end} "
							f"next_round_in={float(self._tick_seconds):.1f}s"
						)
					else:
						bt.logging.info(
							f"⏱️ Timed mode heartbeat: block={current_block} next_round_in={float(self._tick_seconds):.1f}s"
						)
				except Exception:
					pass
				await self.sleep(self._tick_seconds)
				return

			# Default epoch-aware mode (production-like).
			await self._run_round_cycle(ignore_epoch_gating=False)
			await self.sleep(self.round_cadence)
			return
		except KeyboardInterrupt:
			bt.logging.info("⏹️ Validator interrupted by user")
			raise
		except Exception as exc:
			bt.logging.error(f"✗ [FATAL] Unexpected error in forward(): {exc}", exc_info=True)
			await self.sleep(self.round_cadence)
			return

	async def _run_round_cycle(self, *, ignore_epoch_gating: bool) -> None:
		round_started = False
		round_completed = False
		round_id: Optional[str] = None
		tasks: List[Any] = []
		active_uids: List[int] = []
		responses: Dict[int, Any] = {}
		scores: Dict[int, float] = {}

		try:
			# Refresh metagraph to pick up newly registered miners
			if self.subtensor is not None and self.metagraph is not None:
				try:
					now = time.time()
					if now - float(getattr(self, "_last_metagraph_sync_at", 0.0) or 0.0) >= float(
						getattr(self, "_metagraph_resync_seconds", 60.0) or 60.0
					):
						self._with_subtensor(self.metagraph.sync, subtensor=self.subtensor, lite=False)
						self._last_metagraph_sync_at = now
						bt.logging.info("🔁 Metagraph resync completed")
				except Exception as exc:
					bt.logging.debug(f"Failed to refresh metagraph: {exc}")

			try:
				current_block = int(self.block)
			except Exception as exc:
				bt.logging.warning(f"⚠️ Could not get current block: {exc}")
				return

			if not ignore_epoch_gating:
				should_start = bool(self.round_manager.should_start_new_round(current_block))
				if not should_start:
					self._maybe_log_epoch_gate(
						current_block=current_block,
						started=False,
						reason="should_start_new_round=false",
					)
					return

				# By default, run one round per epoch in epoch-aware mode.
				if self._one_round_per_epoch:
					try:
						current_epoch = int(self.round_manager.get_current_epoch(current_block))
						if self._last_round_epoch_started == current_epoch:
							self._maybe_log_epoch_gate(
								current_block=current_block,
								started=False,
								reason="one_round_per_epoch_already_started",
							)
							return
					except Exception:
						pass

				if self.round_manager.tempo > 0:
					current_epoch = self.round_manager.get_current_epoch(current_block)
					epoch_start_block = current_epoch * self.round_manager.tempo
					blocks_into_epoch = current_block - epoch_start_block
					progress = blocks_into_epoch / float(self.round_manager.tempo)

					epoch_slots = max(1, int(getattr(self, "_epoch_slots", 1) or 1))
					if epoch_slots > 1:
						slot_index = self._epoch_slot_index(epoch_slots)
						window_start = float(slot_index) / float(epoch_slots)
						window_end = float(slot_index + 1) / float(epoch_slots)
						if not (window_start <= progress < window_end):
							self._maybe_log_epoch_gate(
								current_block=current_block,
								started=False,
								reason=f"epoch_slot_window slot={slot_index}/{epoch_slots} window={window_start:.2f}-{window_end:.2f}",
							)
							return
					elif self.skip_round_if_started_after_fraction is not None:
						if progress > self.skip_round_if_started_after_fraction:
							bt.logging.info(
								f"⏭️ Skipping round: epoch progress {progress:.2f} exceeds threshold {self.skip_round_if_started_after_fraction:.2f}"
							)
							self._maybe_log_epoch_gate(
								current_block=current_block,
								started=False,
								reason="skip_round_if_started_after_fraction",
							)
							return
				self._maybe_log_epoch_gate(
					current_block=current_block,
					started=True,
					reason=(
						f"starting_round slot={self._epoch_slot_index(self._epoch_slots)}/{int(self._epoch_slots)}"
						if int(getattr(self, "_epoch_slots", 1) or 1) > 1
						else "starting_round"
					),
				)
				if self._one_round_per_epoch:
					try:
						self._last_round_epoch_started = int(self.round_manager.get_current_epoch(current_block))
					except Exception:
						pass

			round_id = self.get_current_round_id()
			# Ensure all mixins use a stable, shared round_id for this cycle.
			# (In non-local-test mode, _run_generation_phase also sets this.)
			try:
				self._current_round_id = round_id
			except Exception:
				pass
			try:
				scores = np.asarray(self.scores, dtype=np.float32)
				non_zero = int(np.count_nonzero(scores))
				max_score = float(np.max(scores)) if scores.size else 0.0
				bt.logging.info(
					f"📊 Scores snapshot: non_zero={non_zero}/{scores.size} max={max_score:.4f}"
				)
				if scores.size:
					non_zero_entries = [
						f"{uid}={float(scores[uid]):.4f}"
						for uid in range(int(scores.size))
						if float(scores[uid]) != 0.0
					]
					zero_count = int(scores.size) - len(non_zero_entries)
					if non_zero_entries:
						entries = "\n".join(non_zero_entries)
						bt.logging.info(
							f"📊 Scores(non-zero):\n{entries}\nnum uids with zero score: {zero_count}"
						)
					else:
						bt.logging.info(
							f"📊 Scores(non-zero): none | num uids with zero score: {zero_count}"
						)
			except Exception:
				pass
			try:
				self._round_lockfile_path.parent.mkdir(parents=True, exist_ok=True)
				payload = {
					"round_id": str(round_id),
					"block": int(current_block),
					"ts": int(time.time()),
					"pid": int(os.getpid()),
				}
				self._round_lockfile_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
			except Exception:
				pass
			self.round_manager.start_round(round_id=round_id, current_block=current_block)
			round_started = True

			# Phase 1: tasks
			if self._local_test_enabled:
				from modules.models import ACTaskSpec, ACPolicy, VerifyPlan

				# Use the canonical sandbox task.json so the validation API can score the
				# fixture zips (miner-result.zip / miner-bad.zip) deterministically.
				validation_task_json = None
				try:
					alphacore_root = Path(__file__).resolve().parents[1]
					task_json_path = (
						alphacore_root
						/ "modules"
						/ "evaluation"
						/ "validation"
						/ "sandbox"
						/ "test_zip"
						/ "task.json"
					)
					if task_json_path.is_file():
						validation_task_json = json.loads(task_json_path.read_text(encoding="utf-8"))
				except Exception:
					validation_task_json = None

				task_id = f"local-test-{int(time.time())}"
				prompt = "Local test: submit fixture ZIP to validation API"
				if self._local_test_use_taskgen_prompt:
					try:
						from modules.generation import TaskGenerationPipeline

						if self._local_test_prompt_pipeline is None:
							self._local_test_prompt_pipeline = TaskGenerationPipeline(
								validator_sa=self.wallet.hotkey.ss58_address
							)
						gen_task = self._local_test_prompt_pipeline.generate()
						gen_prompt = getattr(gen_task, "prompt", None)
						if isinstance(gen_prompt, str) and gen_prompt.strip():
							prompt = gen_prompt.strip()
							bt.logging.info("🧪 Local test: using task generation pipeline prompt")
						else:
							bt.logging.warning("🧪 Local test: task generation pipeline returned empty prompt")
					except Exception as exc:
						bt.logging.warning(f"🧪 Local test: failed to generate prompt via task pipeline: {exc}")
				tasks = [
					ACTaskSpec(
						task_id=task_id,
						provider="local",
						kind="local_test",
						params={"validation_task_json": validation_task_json} if isinstance(validation_task_json, dict) else {},
						policy=ACPolicy(description="local test", max_cost="low", constraints={}),
						verify_plan=VerifyPlan(kind="noop", steps=[]),
						cost_tier="low",
						prompt=prompt,
						verify_fn=None,
					)
				]
			else:
				tasks = await self._run_generation_phase(round_id=round_id)

			# Phase 2: targets
			targets: List[tuple[int, bt.AxonInfo]] = []
			# In local-test mode we still prefer metagraph-discovered miners so local runs
			# behave like production (registration + serve_axon + metagraph discovery).
			await self._run_handshake_phase(round_id)
			active_uids = self.get_active_miner_uids(round_id)
			targets = [(uid, self.metagraph.axons[uid]) for uid in active_uids if uid < len(self.metagraph.axons)]

			if not targets and self._local_miners_fallback_enabled():
				# If miners failed to serve axons to chain, metagraph will show 0.0.0.0:0.
				# For local development we can still target local axons directly.
				try:
					targets = self._load_local_miner_targets()
				except Exception:
					targets = []

			if not targets:
				try:
					now = time.time()
					last_log = float(getattr(self, "_last_no_targets_log_at", 0.0) or 0.0)
					if now - last_log >= 30.0:
						netuid = getattr(self.config, "netuid", None)
						bt.logging.warning(
							f"No active miners after handshake; skipping cycle (round={round_id}, block={current_block}, netuid={netuid})"
						)
						self._last_no_targets_log_at = now
				except Exception:
					pass
				return

			# Phase 3: dispatch
			responses = await self._run_dispatch_phase(tasks, targets=targets)

			# Phase 5: evaluation (works for smoke zip responses)
			scores = await self._run_consensus_phase(tasks, responses)
			try:
				self._weights_tasks_completed += len(tasks or [])
			except Exception:
				pass

			# Phase 6: feedback (only for real UIDs on metagraph)
			try:
				latency_map = self.get_latencies(round_id) or {}
				for task in tasks:
					task_id = task.get("task_id") if isinstance(task, dict) else getattr(task, "task_id", "")
					if not task_id:
						continue
					task_scores = {uid: float(scores.get(uid, 0.0)) for uid, _ in targets if uid >= 0}
					task_latencies = {
						uid: float(latency_map.get((uid, task_id), 0.0))
						for uid, _ in targets
						if uid >= 0
					}
					if task_scores:
						await self.send_task_feedback(
							round_id=round_id,
							task_id=task_id,
							scores=task_scores,
							latencies=task_latencies,
						)
			except Exception as exc:
				bt.logging.debug(f"Feedback skipped/failed: {exc}")

			# Phase 6b: cleanup (send validation results back to miners).
			try:
				validation_results = {}
				try:
					validation_results = self.get_validation_results(round_id) or {}
				except Exception:
					validation_results = {}

				for task in tasks:
					task_id = task.get("task_id") if isinstance(task, dict) else getattr(task, "task_id", "")
					if not task_id:
						continue
					for uid, axon in targets:
						if uid < 0:
							continue
						uid_results = validation_results.get(uid) or {}
						validation_payload = uid_results.get(task_id)
						if not validation_payload:
							continue

						payload_to_send = dict(validation_payload) if isinstance(validation_payload, dict) else validation_payload
						if isinstance(payload_to_send, dict):
							payload_to_send.pop("tap", None)
						cleanup = TaskCleanupSynapse(task_id=task_id, validation_response=payload_to_send)
						await self._dendrite_request(axons=[axon], synapse=cleanup, deserialize=False, timeout=10)
			except Exception as exc:
				bt.logging.debug(f"Cleanup skipped/failed: {exc}")

		# Phase 7: settlement (updates rolling scores)
			try:
				active_uids = [uid for uid, _ in targets if uid >= 0]
				await self._run_settlement_phase(scores, active_uids)
			except Exception as exc:
				bt.logging.debug(f"Settlement skipped/failed: {exc}")

			round_completed = True

		finally:
			if round_started:
				self.round_manager.finish_round()
			try:
				if round_started and getattr(self, "_round_lockfile_path", None) is not None:
					if self._round_lockfile_path.exists():
						self._round_lockfile_path.unlink()
			except Exception:
				pass
			if round_completed and round_id is not None:
				await self._cleanup_round_state(round_id)

	async def _cleanup_round_state(self, round_id: str) -> None:
		"""Clear cached data after a successful round."""
		try:
			from subnet.validator.round_summary import RoundSummaryWriter

			writer = RoundSummaryWriter()
			written = writer.write_from_validator(self, round_id)
			if written is not None:
				bt.logging.info(f"🧾 Round summary written: {written}")
		except Exception as exc:
			bt.logging.debug(f"Round summary skipped/failed for round {round_id}: {exc}")
		self.clear_round_tasks()
		self.clear_task_responses(round_id)
		self.clear_handshake_state(round_id)
		self.clear_feedback_state(round_id)


if __name__ == "__main__":
	cfg = build_config(role="validator")
	validator = Validator(config=cfg)
	try:
		validator.run()
	except KeyboardInterrupt:
		bt.logging.info("✓ Validator shutting down.")
