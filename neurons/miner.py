"""
Starter AlphaCore miner neuron (reference implementation).

This implementation is intentionally minimal and safe: it does not execute
Terraform. It shows how to:
  - Read a prompt-only AlphaCore `TaskSynapse` (`task_id` + natural-language prompt).
  - Return a structured response in `result_summary`.
  - Attach a tiny ZIP artifact (example only).

Miners should implement their own pipeline inside `_handle_task()`:
  parse prompt → generate Terraform → apply → ensure terraform.tfstate → zip workspace
"""

import io
import json
import os
import sys
import time
import threading
import zipfile
from pathlib import Path
from typing import Optional

try:
    import bittensor as bt
except ModuleNotFoundError:  # pragma: no cover
    bt = None  # type: ignore[assignment]

try:
    from subnet.base.miner import BaseMinerNeuron
    from subnet.bittensor_config import config as build_config
    from subnet.protocol import (
        StartRoundSynapse,
        TaskCleanupSynapse,
        TaskFeedbackSynapse,
        TaskSynapse,
    )
except ModuleNotFoundError:  # pragma: no cover
    BaseMinerNeuron = object  # type: ignore[misc,assignment]
    build_config = None  # type: ignore[assignment]
    StartRoundSynapse = object  # type: ignore[assignment]
    TaskCleanupSynapse = object  # type: ignore[assignment]
    TaskFeedbackSynapse = object  # type: ignore[assignment]
    TaskSynapse = object  # type: ignore[assignment]

# Models live under `modules/`, which is not necessarily on the
# default `sys.path` when running this neuron from different working directories.
try:
    from modules.models import ACResult, ACEvidence
except ModuleNotFoundError:  # pragma: no cover - allow import in thin dev envs
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    from modules.models import ACResult, ACEvidence


class Miner(BaseMinerNeuron):
    """
    Minimal AlphaCore miner neuron.

    Implementers should replace `_handle_task()` with their own pipeline:
      parse prompt → generate terraform → apply → ensure terraform.tfstate → zip workspace
    """

    neuron_type: str = "AlphaCoreMinerStarter"

    def __init__(self, config: Optional["bt.Config"] = None) -> None:
        if bt is None or build_config is None:
            raise RuntimeError("Missing runtime dependencies: bittensor / subnet.")
        super().__init__(config=config or build_config(role="miner"))
        bt.logging.info(f"Initialized {self.neuron_type}")
        self._app_heartbeat_stop = threading.Event()
        self._app_heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._app_heartbeat_thread.start()

    # ------------------------------------------------------------------ #
    # Handshake / feedback / cleanup (required axon endpoints)
    # ------------------------------------------------------------------ #

    async def forward_start_round(self, synapse: StartRoundSynapse) -> StartRoundSynapse:
        self._log_incoming(
            "StartRoundSynapse",
            synapse,
            extra={
                "round_id": getattr(synapse, "round_id", ""),
                "timestamp": getattr(synapse, "timestamp", None),
                "validator_version": getattr(synapse, "validator_version", ""),
            },
        )
        synapse.miner_version = "starter"
        synapse.is_ready = True
        synapse.available_capacity = 1
        synapse.error_message = None
        self._log_outgoing(
            "StartRoundSynapse",
            synapse,
            extra={
                "is_ready": bool(getattr(synapse, "is_ready", False)),
                "available_capacity": int(getattr(synapse, "available_capacity", 0) or 0),
            },
        )
        return synapse

    async def forward_feedback(self, synapse: TaskFeedbackSynapse) -> TaskFeedbackSynapse:
        self._log_incoming(
            "TaskFeedbackSynapse",
            synapse,
            extra={
                "round_id": getattr(synapse, "round_id", ""),
                "task_id": getattr(synapse, "task_id", ""),
                "score": getattr(synapse, "score", None),
                "latency_seconds": getattr(synapse, "latency_seconds", None),
                "feedback_text": getattr(synapse, "feedback_text", None),
            },
        )
        synapse.acknowledged = True
        self._log_outgoing("TaskFeedbackSynapse", synapse, extra={"acknowledged": True})
        return synapse

    async def forward_cleanup(self, synapse: TaskCleanupSynapse) -> TaskCleanupSynapse:
        # Default: acknowledge; real miners should tear down any temporary files/resources.
        self._log_incoming(
            "TaskCleanupSynapse",
            synapse,
            extra={
                "task_id": getattr(synapse, "task_id", ""),
                "validation_keys": list(getattr(synapse, "validation_response", {}) or {})[:12],
            },
        )
        synapse.acknowledged = True
        synapse.cleanup_ok = True
        synapse.error_message = None
        self._log_outgoing(
            "TaskCleanupSynapse",
            synapse,
            extra={"acknowledged": True, "cleanup_ok": True, "error_message": None},
        )
        return synapse

    # ------------------------------------------------------------------ #
    # Task endpoint
    # ------------------------------------------------------------------ #

    async def forward(self, synapse: TaskSynapse) -> TaskSynapse:
        """
        Handle a single AlphaCore task.

        Validators score miners primarily from `workspace_zip_b64` (submission ZIP).
        This starter implementation returns a structured response and can optionally
        attach a tiny ZIP artifact (example transport only).
        """
        start = time.time()

        try:
            try:
                self._tasks_handled = int(getattr(self, "_tasks_handled", 0) or 0) + 1
                self._last_task_at = float(time.time())
                self._last_task_id = str(getattr(synapse, "task_id", "") or "")
            except Exception:
                pass

            prompt = self._extract_prompt(synapse)
            self._log_incoming("TaskSynapse", synapse, extra={"task_id": getattr(synapse, "task_id", "")})
            self._log_prompt(prompt)

            result, evidence = await self._handle_task(task_id=synapse.task_id, prompt=prompt)
            synapse.attach_result(result, evidence=evidence)

            # Attach a minimal ZIP artifact for end-to-end transport testing.
            # This is not a scored submission, but it is a concrete example of the expected ZIP transport.
            # Disable by setting ALPHACORE_MINER_RETURN_EXAMPLE_ZIP=0.
            if self._env_flag("ALPHACORE_MINER_RETURN_EXAMPLE_ZIP", default=True):
                if self._env_flag("ALPHACORE_MINER_RETURN_DUMMY_ZIP"):
                    zip_bytes = self._build_dummy_zip(task_id=synapse.task_id, prompt=prompt)
                else:
                    zip_bytes = self._build_example_zip(task_id=synapse.task_id, prompt=prompt)
                synapse.attach_workspace_zip_bytes(zip_bytes, filename=f"{synapse.task_id}.zip")

            synapse.notes = f"handled_by={self.neuron_type} latency_s={time.time() - start:.3f}"
            self._log_outgoing(
                "TaskSynapse",
                synapse,
                extra={
                    "task_id": getattr(synapse, "task_id", ""),
                    "latency_s": float(time.time() - start),
                    "workspace_zip_sha256": getattr(synapse, "workspace_zip_sha256", None),
                    "workspace_zip_size_bytes": getattr(synapse, "workspace_zip_size_bytes", None),
                    "result_status": (getattr(synapse, "result_summary", {}) or {}).get("status"),
                },
            )
            return synapse
        except Exception as exc:
            bt.logging.error(f"Task failed: task_id={getattr(synapse, 'task_id', None)} err={exc}")
            synapse.attach_result(
                ACResult(
                    task_id=str(getattr(synapse, "task_id", "") or ""),
                    status="error",
                    notes=str(exc),
                )
            )
            synapse.notes = f"error latency_s={time.time() - start:.3f}"
            self._log_outgoing(
                "TaskSynapse",
                synapse,
                extra={
                    "task_id": getattr(synapse, "task_id", ""),
                    "latency_s": float(time.time() - start),
                    "error": str(exc),
                },
            )
            return synapse

    async def _handle_task(self, *, task_id: str, prompt: str) -> tuple[ACResult, Optional[ACEvidence]]:
        """
        Placeholder execution.

        Replace this with your real miner logic that produces a repository-root Terraform
        project + `terraform.tfstate`, bundled into the returned ZIP.
        """
        _ = prompt
        return (
            ACResult(
                task_id=task_id,
                status="not_implemented",
                notes=(
                    "Starter miner: implement _handle_task() to generate a real submission ZIP. "
                    "This miner currently returns an example ZIP (see ALPHACORE_MINER_RETURN_EXAMPLE_ZIP)."
                ),
            ),
            ACEvidence(task_id=task_id, attachments={"kind": "starter", "note": "example zip only"}),
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_prompt(synapse: TaskSynapse) -> str:
        prompt = (getattr(synapse, "prompt", None) or "").strip()
        if prompt:
            return prompt
        task_spec = getattr(synapse, "task_spec", None)
        if isinstance(task_spec, dict):
            maybe = task_spec.get("prompt")
            if isinstance(maybe, str) and maybe.strip():
                return maybe.strip()
        return ""

    @staticmethod
    def _env_flag(name: str, *, default: bool = False) -> bool:
        raw = os.getenv(name, None)
        if raw is None or raw == "":
            return bool(default)
        return raw.strip().lower() in ("1", "true", "yes", "y", "on")

    @staticmethod
    def _safe_preview(text: str, *, limit: int = 240) -> str:
        cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not cleaned:
            return ""
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[:limit] + "…"

    @staticmethod
    def _log_human_block(prefix: str, lines: list[str]) -> None:
        if bt is None:
            return
        for line in lines:
            try:
                bt.logging.info(f"{prefix}{line}")
            except Exception:
                pass

    def _dendrite_summary(self, synapse: object) -> str:
        dend = getattr(synapse, "dendrite", None)
        if dend is None:
            return "from=?"
        hotkey = getattr(dend, "hotkey", None)
        ip = getattr(dend, "ip", None)
        port = getattr(dend, "port", None)
        hotkey_short = f"{str(hotkey)[:10]}…" if hotkey else "?"
        ip_part = f"{ip}:{port}" if ip else "?"
        return f"from={hotkey_short} ip={ip_part}"

    def _log_incoming(self, kind: str, synapse: object, *, extra: Optional[dict] = None) -> None:
        if bt is None:
            return
        uid = int(getattr(self, "uid", -1))
        bits = [f"<-- {kind:<18} uid={uid} {self._dendrite_summary(synapse)}"]
        extra = extra or {}
        if kind == "TaskSynapse":
            task_id = str(extra.get("task_id") or getattr(synapse, "task_id", "") or "")
            if task_id:
                bits.append(f"task_id={task_id}")
        if kind == "TaskFeedbackSynapse":
            task_id = str(extra.get("task_id") or getattr(synapse, "task_id", "") or "")
            score = extra.get("score", getattr(synapse, "score", None))
            if task_id:
                bits.append(f"task_id={task_id}")
            if score is not None:
                bits.append(f"score={float(score):.4f}")
        if kind == "StartRoundSynapse":
            round_id = str(extra.get("round_id") or getattr(synapse, "round_id", "") or "")
            validator_version = str(
                extra.get("validator_version")
                or getattr(synapse, "validator_version", "")
                or ""
            )
            if round_id:
                bits.append(f"round_id={round_id}")
            if validator_version:
                bits.append(f"validator_version={validator_version}")
        if kind == "TaskCleanupSynapse":
            task_id = str(extra.get("task_id") or getattr(synapse, "task_id", "") or "")
            if task_id:
                bits.append(f"task_id={task_id}")
        self._log_human_block("", [" ".join(bits)])

    def _log_outgoing(self, kind: str, synapse: object, *, extra: Optional[dict] = None) -> None:
        if bt is None:
            return
        uid = int(getattr(self, "uid", -1))
        extra = extra or {}
        bits = [f"--> {kind:<18} uid={uid}"]
        if kind == "TaskSynapse":
            task_id = str(extra.get("task_id") or getattr(synapse, "task_id", "") or "")
            status = str(extra.get("result_status") or (getattr(synapse, "result_summary", {}) or {}).get("status") or "")
            zip_bytes = extra.get("workspace_zip_size_bytes", getattr(synapse, "workspace_zip_size_bytes", None))
            if task_id:
                bits.append(f"task_id={task_id}")
            if status:
                bits.append(f"status={status}")
            if zip_bytes is not None:
                bits.append(f"zip_bytes={int(zip_bytes)}")
        if kind == "TaskFeedbackSynapse":
            task_id = str(getattr(synapse, "task_id", "") or "")
            score = getattr(synapse, "score", None)
            if task_id:
                bits.append(f"task_id={task_id}")
            if score is not None:
                bits.append(f"score={float(score):.4f}")
            bits.append("ack=true")
        if kind == "StartRoundSynapse":
            bits.append(f"is_ready={bool(getattr(synapse, 'is_ready', False))}")
            bits.append(f"capacity={int(getattr(synapse, 'available_capacity', 0) or 0)}")
        if kind == "TaskCleanupSynapse":
            bits.append(f"ack={bool(getattr(synapse, 'acknowledged', False))}")
            bits.append(f"cleanup_ok={bool(getattr(synapse, 'cleanup_ok', False))}")
        self._log_human_block("", [" ".join(bits)])

    def _log_prompt(self, prompt: str) -> None:
        prompt = (prompt or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not prompt:
            self._log_human_block("", ["prompt=(empty)"])
            return

        try:
            max_chars = int(os.getenv("ALPHACORE_MINER_PROMPT_LOG_MAX_CHARS", "2500") or "2500")
        except Exception:
            max_chars = 2500
        max_chars = max(200, max_chars)

        one_line = " ".join(prompt.split())
        if len(one_line) > max_chars:
            self._log_human_block("", [f"prompt={one_line[:max_chars]}… (truncated, total_chars={len(one_line)})"])
            return
        self._log_human_block("", [f"prompt={one_line}"])

    @staticmethod
    def _build_dummy_zip(*, task_id: str, prompt: str) -> bytes:
        """
        Build a tiny ZIP artifact for wiring tests.

        This is NOT a valid submission for scoring; it's just a transport example.
        """
        payload = {"task_id": task_id, "prompt": prompt, "status": "dummy"}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("output.json", json.dumps(payload, indent=2, sort_keys=True))
            zf.writestr("README.txt", "AlphaCore starter miner artifact (dummy)\n")
        return buf.getvalue()

    @staticmethod
    def _build_example_zip(*, task_id: str, prompt: str) -> bytes:
        """
        Build a tiny Terraform workspace ZIP that is safe to run.

        The config intentionally creates no resources (no providers needed), so
        `terraform init` / `terraform apply` can succeed without downloading plugins.
        """
        tf_main = (
            'terraform {\n'
            '  required_version = ">= 1.5.0"\n'
            '}\n'
            '\n'
            'output "alphacore_miner_example" {\n'
            '  value = "hello-from-starter-miner"\n'
            '}\n'
        )
        meta = {
            "task_id": task_id,
            "kind": "starter_example",
            "prompt_preview": (prompt or "")[:240],
            "created_at_unix": int(time.time()),
        }

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("main.tf", tf_main)
            zf.writestr(
                "README.md",
                (
                    "# AlphaCore Starter Miner Submission (Example)\n\n"
                    "This ZIP is an example artifact returned by the starter miner.\n"
                    "It contains a minimal Terraform project that creates no resources.\n"
                    "Replace this with your own Terraform code + real `terraform.tfstate` output.\n"
                ),
            )
            zf.writestr("alphacore_miner_meta.json", json.dumps(meta, indent=2, sort_keys=True))
        return buf.getvalue()

    def _heartbeat_loop(self) -> None:
        if bt is None:
            return
        try:
            interval_s = float(os.getenv("ALPHACORE_MINER_APP_HEARTBEAT_SECONDS", "60") or "60")
        except Exception:
            interval_s = 60.0
        interval_s = max(1.0, float(interval_s))

        while not self._app_heartbeat_stop.is_set() and not bool(getattr(self, "should_exit", False)):
            try:
                tasks_handled = int(getattr(self, "_tasks_handled", 0) or 0)
                last_task_id = getattr(self, "_last_task_id", None)
                hotkey = None
                try:
                    hotkey = str(getattr(getattr(self.wallet, "hotkey", None), "ss58_address", None))
                except Exception:
                    hotkey = None
                bt.logging.info(
                    f"hb uid={int(getattr(self, 'uid', -1))} hotkey={hotkey} "
                    f"tasks={tasks_handled} last_task_id={str(last_task_id) if last_task_id else 'null'}"
                )
            except Exception:
                pass
            self._app_heartbeat_stop.wait(interval_s)


if __name__ == "__main__":
    if bt is None or build_config is None:
        raise SystemExit("bittensor is not installed; cannot run miner.")
    cfg = build_config(role="miner")
    with Miner(config=cfg) as miner:
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            bt.logging.info("Miner shutting down.")
