from __future__ import annotations

import asyncio
import json
import logging
import os
import hashlib
import shutil
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from uvicorn import run as uvicorn_run

from modules.evaluation.validation.sandbox.token_manager import GcpAccessTokenManager
from modules.evaluation.validation.sandbox.worker_pool import SandboxJob, SandboxWorkerPool


logger = logging.getLogger("alphacore.validation_api")


class HealthCheckResponse(BaseModel):
    status: str
    sandbox_ready: bool
    sandbox_workers: int
    sandbox_queue_size: int
    sandbox_queued: int
    sandbox_running: int
    token_ready: bool
    token_error: Optional[str] = None
    timestamp: str


class ValidationSubmitRequest(BaseModel):
    workspace_zip_path: str
    task_json: dict[str, Any]
    timeout_s: int = 120
    net_checks: bool = False
    stream_log: bool = False
    quiet_kernel: bool = True


class ValidationSubmitResponse(BaseModel):
    job_id: str
    task_id: Optional[str] = None
    result: dict[str, Any]
    log_url: str
    log_path: str
    submission_path: str
    tap: Optional[str] = None


class ValidationJobStatusResponse(BaseModel):
    job_id: str
    status: str
    queued_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    log_path: Optional[str] = None
    log_tail: Optional[str] = None


def _configure_logging() -> None:
    if logger.handlers:
        return
    level = os.getenv("ALPHACORE_VALIDATION_API_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")


def create_app() -> FastAPI:
    _configure_logging()

    sandbox_workers = int(os.getenv("ALPHACORE_SANDBOX_WORKERS", "4"))
    sandbox_queue_size = int(os.getenv("ALPHACORE_SANDBOX_QUEUE_SIZE", "64"))
    archive_root = os.environ.get("ALPHACORE_VALIDATION_ARCHIVE_ROOT")
    token_creds_file = os.getenv("ALPHACORE_GCP_CREDS_FILE")
    log_dir = Path(os.getenv("ALPHACORE_VALIDATION_LOG_DIR", "./logs/validation")).resolve()
    active_log_dir = Path(os.getenv("ALPHACORE_VALIDATION_ACTIVE_LOG_DIR", str(log_dir / "active"))).resolve()
    log_by_task_dir = Path(os.getenv("ALPHACORE_VALIDATION_LOG_BY_TASK_DIR", str(log_dir / "by_task"))).resolve()
    log_by_miner_dir = Path(os.getenv("ALPHACORE_VALIDATION_LOG_BY_MINER_DIR", str(log_dir / "by_miner"))).resolve()
    submissions_dir = Path(os.getenv("ALPHACORE_VALIDATION_SUBMISSIONS_DIR", str(log_dir / "submissions"))).resolve()
    submissions_by_task_dir = Path(os.getenv("ALPHACORE_VALIDATION_SUBMISSIONS_BY_TASK_DIR", str(submissions_dir / "by_task"))).resolve()
    submissions_by_miner_dir = Path(
        os.getenv("ALPHACORE_VALIDATION_SUBMISSIONS_BY_MINER_DIR", str(submissions_dir / "by_miner"))
    ).resolve()
    log_tail_lines = int(os.getenv("ALPHACORE_VALIDATION_LOG_TAIL_LINES", "200"))

    token_manager = GcpAccessTokenManager(creds_file=Path(token_creds_file).resolve() if token_creds_file else None)
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(maxsize=max(1, sandbox_queue_size))
    jobs: dict[str, dict[str, object]] = {}
    jobs_lock = asyncio.Lock()
    workers: list[asyncio.Task] = []
    running_counter = {"running": 0}
    pool_holder: dict[str, object] = {"pool": None}

    def _safe_name(value: str) -> str:
        out = []
        for ch in value:
            if ch.isalnum() or ch in {"-", "_", "."}:
                out.append(ch)
            else:
                out.append("_")
        return "".join(out)[:80] or "task"

    def _tail_log(path: Path, lines: int) -> str:
        try:
            dq: deque[str] = deque(maxlen=max(1, lines))
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    dq.append(line.rstrip("\n"))
            return "\n".join(dq)
        except Exception:
            return ""

    async def sandbox_worker_loop(worker_id: int) -> None:
        while True:
            job_id, job_spec = await queue.get()
            task_id = str(job_spec.get("task_id") or "")
            async with jobs_lock:
                running_counter["running"] += 1
                record = jobs.get(job_id, {})
                record.update({"status": "running", "started_at": datetime.utcnow().isoformat()})
                jobs[job_id] = record
            logger.info(
                "job_start job_id=%s task_id=%s worker=%s log_path=%s",
                job_id,
                task_id,
                worker_id,
                job_spec.get("log_path"),
            )

            try:
                access_token = await token_manager.get_token()
                pool = pool_holder["pool"]
                assert isinstance(pool, SandboxWorkerPool)
                job_log_path = Path(job_spec["log_path"]).resolve()
                submission_path = Path(job_spec["submission_path"]).resolve()
                done_future = job_spec.get("future")
                job = SandboxJob(
                    job_id=job_id,
                    workspace_zip=submission_path,
                    task_json=job_spec["task_json"],
                    timeout_s=int(job_spec["timeout_s"]),
                    net_checks=bool(job_spec["net_checks"]),
                    stream_log=bool(job_spec["stream_log"]),
                    quiet_kernel=bool(job_spec["quiet_kernel"]),
                    log_path=job_log_path,
                    env={"GOOGLE_OAUTH_ACCESS_TOKEN": access_token},
                )
                result = await pool.run_one(job)
                result_summary = result.summary if isinstance(result.summary, dict) else {}
                if result_summary.get("success_json"):
                    result_obj = result_summary.get("success_json")
                elif result_summary.get("error_json"):
                    result_obj = result_summary.get("error_json")
                elif result_summary.get("success") is True:
                    result_obj = {"status": "pass", "score": result_summary.get("score", 1.0)}
                else:
                    result_obj = {"msg": result_summary.get("error") or "Unknown error", "score": result_summary.get("score", 0)}

                if isinstance(result_obj, dict):
                    # Normalize result shape: always include status pass|fail.
                    status_value = str(result_obj.get("status") or "")
                    if status_value not in {"pass", "fail"}:
                        result_obj["status"] = "pass" if result.returncode == 0 else "fail"
                    if result.returncode != 0 and "msg" not in result_obj and "error" in result_obj:
                        result_obj["msg"] = result_obj.get("error")

                async with jobs_lock:
                    record = jobs.get(job_id, {})
                    record.update(
                        {
                            "status": "done" if result.returncode == 0 else "failed",
                            "finished_at": datetime.utcnow().isoformat(),
                            "result": result.summary,
                            "log_path": str(job_log_path),
                            "submission_path": str(submission_path),
                            "log_tail": result.stdout_tail,
                        }
                    )
                    jobs[job_id] = record
                logger.info(
                    "job_done job_id=%s task_id=%s rc=%s status=%s tap=%s log_path=%s",
                    job_id,
                    task_id,
                    result.returncode,
                    (result_obj or {}).get("status") if isinstance(result_obj, dict) else "",
                    result_summary.get("tap"),
                    str(job_log_path),
                )

                if done_future is not None and not done_future.done():
                    done_future.set_result({"result": result_obj, "tap": result_summary.get("tap")})
            except Exception as exc:
                async with jobs_lock:
                    record = jobs.get(job_id, {})
                    record.update(
                        {
                            "status": "failed",
                            "finished_at": datetime.utcnow().isoformat(),
                            "error": str(exc),
                            "log_path": str(job_spec.get("log_path", "")) or None,
                            "submission_path": str(job_spec.get("submission_path", "")) or None,
                        }
                    )
                    jobs[job_id] = record
                logger.exception("job_error job_id=%s task_id=%s err=%s", job_id, task_id, exc)
                try:
                    done_future = job_spec.get("future")
                    if done_future is not None and not done_future.done():
                        done_future.set_result({"result": {"status": "fail", "msg": str(exc), "score": 0}, "tap": None})
                except Exception:
                    pass
            finally:
                try:
                    active_path = Path(str(job_spec.get("active_log_path", ""))).resolve()
                    if active_path.exists() and active_path.is_symlink():
                        active_path.unlink()
                except Exception:
                    pass
                async with jobs_lock:
                    running_counter["running"] = max(0, int(running_counter["running"]) - 1)
                queue.task_done()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log_dir.mkdir(parents=True, exist_ok=True)
        active_log_dir.mkdir(parents=True, exist_ok=True)
        log_by_task_dir.mkdir(parents=True, exist_ok=True)
        log_by_miner_dir.mkdir(parents=True, exist_ok=True)
        submissions_dir.mkdir(parents=True, exist_ok=True)
        submissions_by_task_dir.mkdir(parents=True, exist_ok=True)
        submissions_by_miner_dir.mkdir(parents=True, exist_ok=True)
        # Fail fast: validation cannot run without a token source.
        if token_creds_file is None and not os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN"):
            raise RuntimeError("Missing ALPHACORE_GCP_CREDS_FILE (or GOOGLE_OAUTH_ACCESS_TOKEN for local testing).")

        await token_manager.start()
        # Ensure we can mint/read a token at startup so /validate doesn't fail later.
        await token_manager.get_token()
        use_sudo = os.getenv("ALPHACORE_SANDBOX_USE_SUDO", "true").lower() in {"1", "true", "yes", "on"}
        sudo_bin = Path(os.getenv("ALPHACORE_SANDBOX_SUDO_BIN", "sudo"))
        python_bin = Path(os.getenv("ALPHACORE_SANDBOX_PYTHON", "/usr/bin/python3"))
        pool_holder["pool"] = SandboxWorkerPool(
            max_workers=max(1, sandbox_workers),
            python=python_bin,
            use_sudo=use_sudo,
            sudo_bin=sudo_bin,
        )
        for i in range(max(1, sandbox_workers)):
            workers.append(asyncio.create_task(sandbox_worker_loop(i)))
        logger.info("validation api ready workers=%s queue=%s", sandbox_workers, sandbox_queue_size)
        try:
            yield
        finally:
            await token_manager.stop()
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    app = FastAPI(
        title="AlphaCore Validation API",
        description="Run sandboxed validation jobs in a bounded worker pool",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_logger(request, call_next):  # type: ignore[no-untyped-def]
        start = time.time()
        response = await call_next(request)
        duration_ms = int((time.time() - start) * 1000)
        job_id = response.headers.get("X-Acore-Job-Id", "")
        req_id = response.headers.get("X-Acore-Request-Id", "")
        try:
            client = request.client.host if request.client else "unknown"
        except Exception:
            client = "unknown"
        logger.info(
            "HTTP %s %s -> %s dur_ms=%s client=%s job_id=%s req_id=%s q=%s running=%s",
            request.method,
            request.url.path,
            getattr(response, "status_code", "unknown"),
            duration_ms,
            client,
            job_id,
            req_id,
            queue.qsize(),
            int(running_counter["running"]),
        )
        return response

    @app.get("/health", response_model=HealthCheckResponse)
    async def health_check() -> HealthCheckResponse:
        token_status = token_manager.status()
        async with jobs_lock:
            queued = queue.qsize()
            running = int(running_counter["running"])
            ready = pool_holder["pool"] is not None
        return HealthCheckResponse(
            status="healthy",
            sandbox_ready=bool(ready),
            sandbox_workers=max(1, sandbox_workers),
            sandbox_queue_size=max(1, sandbox_queue_size),
            sandbox_queued=queued,
            sandbox_running=running,
            token_ready=bool(token_status.token),
            token_error=token_status.last_error,
            timestamp=datetime.utcnow().isoformat(),
        )

    @app.post("/validate", response_model=ValidationSubmitResponse)
    async def submit_validation(request: ValidationSubmitRequest, response: Response) -> ValidationSubmitResponse:
        if pool_holder["pool"] is None:
            raise HTTPException(status_code=503, detail="Sandbox worker pool not initialized")
        try:
            await token_manager.get_token()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Token manager not ready: {exc}")

        zip_path = request.workspace_zip_path
        if not zip_path.lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="workspace_zip_path must end with .zip")
        if not os.path.isfile(zip_path):
            raise HTTPException(status_code=400, detail=f"workspace_zip_path is not a file: {zip_path}")

        if archive_root:
            root = Path(archive_root).resolve()
            candidate = Path(zip_path).resolve()
            if root not in candidate.parents and candidate != root:
                raise HTTPException(status_code=403, detail="workspace_zip_path is outside ALPHACORE_VALIDATION_ARCHIVE_ROOT")

        if queue.full():
            response.headers["Retry-After"] = "1"
            raise HTTPException(status_code=429, detail="Validator is busy; queue is full")

        job_id = uuid.uuid4().hex
        request_id = uuid.uuid4().hex[:12]
        task_id_raw = request.task_json.get("task_id") if isinstance(request.task_json, dict) else None
        task_id = str(task_id_raw) if task_id_raw else None
        miner_uid_raw = request.task_json.get("miner_uid") if isinstance(request.task_json, dict) else None
        miner_uid = str(miner_uid_raw) if miner_uid_raw is not None else None
        invariants_count = 0
        try:
            invariants = request.task_json.get("invariants") if isinstance(request.task_json, dict) else None
            invariants_count = len(invariants) if isinstance(invariants, list) else 0
        except Exception:
            invariants_count = 0

        prefix = _safe_name(task_id) + "__" if task_id else ""
        job_log_path = (log_dir / f"{prefix}{job_id}.log").resolve()
        job_active_link = (active_log_dir / f"{prefix}{job_id}.log").resolve()
        submission_path = (submissions_dir / f"{prefix}{job_id}.zip").resolve()
        submission_meta_path = (submissions_dir / f"{prefix}{job_id}.json").resolve()
        done_future: asyncio.Future = asyncio.get_running_loop().create_future()

        # Persist the submission zip so it can be audited later and found by task_id.
        submissions_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = submission_path.with_suffix(".zip.tmp")
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        try:
            # Try hardlink first (fast), fallback to copy.
            try:
                os.link(Path(zip_path).resolve(), tmp_path)
            except OSError:
                shutil.copy2(Path(zip_path).resolve(), tmp_path)
            tmp_path.replace(submission_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to persist submission: {exc}")

        sha256 = hashlib.sha256()
        try:
            with submission_path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    sha256.update(chunk)
            submission_hash = sha256.hexdigest()
        except Exception:
            submission_hash = ""

        try:
            submission_meta_path.write_text(
                json.dumps(
                    {
                        "job_id": job_id,
                        "task_id": task_id,
                        "miner_uid": miner_uid,
                        "received_at": datetime.utcnow().isoformat(),
                        "original_path": str(Path(zip_path).resolve()),
                        "stored_path": str(submission_path),
                        "sha256": submission_hash,
                        "bytes": submission_path.stat().st_size if submission_path.exists() else None,
                    }
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

        # Index by task_id (best-effort) using symlinks for quick lookup.
        if task_id:
            try:
                by_task = (submissions_by_task_dir / _safe_name(task_id)).resolve()
                by_task.mkdir(parents=True, exist_ok=True)
                link = by_task / f"{job_id}.zip"
                if link.exists() or link.is_symlink():
                    link.unlink()
                link.symlink_to(submission_path)
                link_meta = by_task / f"{job_id}.json"
                if link_meta.exists() or link_meta.is_symlink():
                    link_meta.unlink()
                link_meta.symlink_to(submission_meta_path)
            except Exception:
                pass

            try:
                by_task_log = (log_by_task_dir / _safe_name(task_id)).resolve()
                by_task_log.mkdir(parents=True, exist_ok=True)
                link_log = by_task_log / f"{job_id}.log"
                if link_log.exists() or link_log.is_symlink():
                    link_log.unlink()
                link_log.symlink_to(job_log_path)
            except Exception:
                pass

        if miner_uid:
            try:
                by_miner = (submissions_by_miner_dir / _safe_name(miner_uid)).resolve()
                by_miner.mkdir(parents=True, exist_ok=True)
                link = by_miner / f"{prefix}{job_id}.zip"
                if link.exists() or link.is_symlink():
                    link.unlink()
                link.symlink_to(submission_path)
                link_meta = by_miner / f"{prefix}{job_id}.json"
                if link_meta.exists() or link_meta.is_symlink():
                    link_meta.unlink()
                link_meta.symlink_to(submission_meta_path)
            except Exception:
                pass

            try:
                by_miner_log = (log_by_miner_dir / _safe_name(miner_uid)).resolve()
                by_miner_log.mkdir(parents=True, exist_ok=True)
                link_log = by_miner_log / f"{prefix}{job_id}.log"
                if link_log.exists() or link_log.is_symlink():
                    link_log.unlink()
                link_log.symlink_to(job_log_path)
            except Exception:
                pass

        job_spec = {
            "workspace_zip_path": str(submission_path),
            "submission_path": str(submission_path),
            "task_json": request.task_json,
            "task_id": task_id,
            "timeout_s": max(1, int(request.timeout_s)),
            "net_checks": bool(request.net_checks),
            "stream_log": bool(request.stream_log),
            "quiet_kernel": bool(request.quiet_kernel),
            "log_path": str(job_log_path),
            "active_log_path": str(job_active_link),
            "future": done_future,
        }

        async with jobs_lock:
            jobs[job_id] = {
                "status": "queued",
                "queued_at": datetime.utcnow().isoformat(),
                "task_id": task_id,
                "log_path": str(job_log_path),
                "submission_path": str(submission_path),
                "log_tail": None,
            }
        logger.info(
            "job_queued job_id=%s task_id=%s invariants=%s zip=%s stored_zip=%s log_path=%s",
            job_id,
            task_id or "",
            invariants_count,
            Path(zip_path).name,
            submission_path.name,
            str(job_log_path),
        )

        try:
            active_log_dir.mkdir(parents=True, exist_ok=True)
            if job_active_link.exists() or job_active_link.is_symlink():
                job_active_link.unlink()
            job_active_link.symlink_to(job_log_path)
        except Exception:
            pass

        try:
            queue.put_nowait((job_id, job_spec))
        except asyncio.QueueFull:
            async with jobs_lock:
                jobs.pop(job_id, None)
            try:
                if job_active_link.exists() or job_active_link.is_symlink():
                    job_active_link.unlink()
            except Exception:
                pass
            response.headers["Retry-After"] = "1"
            raise HTTPException(status_code=429, detail="Validator is busy; queue is full")

        response.headers["X-Acore-Job-Id"] = job_id
        response.headers["X-Acore-Request-Id"] = request_id

        try:
            result_payload = await asyncio.wait_for(done_future, timeout=float(job_spec["timeout_s"]) + 30.0)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Timed out waiting for validation result")
        if not isinstance(result_payload, dict):
            raise HTTPException(status_code=500, detail="Invalid validation result payload")
        result_obj = result_payload.get("result") if isinstance(result_payload.get("result"), dict) else {"status": "fail", "msg": "missing result", "score": 0}
        if str(result_obj.get("status") or "") not in {"pass", "fail"}:
            result_obj["status"] = "pass" if jobs.get(job_id, {}).get("status") == "done" else "fail"
        return ValidationSubmitResponse(
            job_id=job_id,
            task_id=task_id,
            result=result_obj,
            log_url=f"/validate/{job_id}/log",
            log_path=str(job_log_path),
            submission_path=str(submission_path),
            tap=result_payload.get("tap"),
        )

    @app.get("/validate/active")
    async def list_active_jobs() -> dict:
        async with jobs_lock:
            active = [
                {"job_id": job_id, "status": record.get("status"), "log_url": f"/validate/{job_id}/log"}
                for job_id, record in jobs.items()
                if record.get("status") in {"queued", "running"}
            ]
        return {"active": active}

    # NOTE: Keep the fixed "/validate/active" route above the dynamic "/validate/{job_id}"
    # routes so Starlette doesn't consume "active" as a job_id.
    @app.get("/validate/{job_id}", response_model=ValidationJobStatusResponse)
    async def get_validation_status(job_id: str) -> ValidationJobStatusResponse:
        async with jobs_lock:
            record = jobs.get(job_id)
        if not record:
            raise HTTPException(status_code=404, detail="Unknown job_id")
        return ValidationJobStatusResponse(
            job_id=job_id,
            status=str(record.get("status", "unknown")),
            queued_at=str(record.get("queued_at", "")),
            started_at=record.get("started_at"),
            finished_at=record.get("finished_at"),
            result=record.get("result"),
            error=record.get("error"),
            log_path=record.get("log_path"),
            log_tail=record.get("log_tail"),
        )

    @app.get("/validate/{job_id}/log", response_class=PlainTextResponse)
    async def get_validation_log(job_id: str, tail: int = 200) -> PlainTextResponse:
        async with jobs_lock:
            record = jobs.get(job_id)
        if not record:
            raise HTTPException(status_code=404, detail="Unknown job_id")
        log_path_raw = record.get("log_path")
        if not log_path_raw:
            raise HTTPException(status_code=404, detail="No log recorded for job")
        path = Path(str(log_path_raw)).resolve()
        if log_dir not in path.parents and path != log_dir:
            raise HTTPException(status_code=403, detail="Log path outside configured log dir")
        if not path.exists():
            raise HTTPException(status_code=404, detail="Log file not found (yet)")
        content = _tail_log(path, min(5000, max(1, int(tail))))
        return PlainTextResponse(content)

    @app.get("/task/{task_id}")
    async def get_task_records(task_id: str) -> dict:
        """Find stored submissions/logs for a task_id."""
        async with jobs_lock:
            matches = [
                {
                    "job_id": job_id,
                    "status": record.get("status"),
                    "queued_at": record.get("queued_at"),
                    "started_at": record.get("started_at"),
                    "finished_at": record.get("finished_at"),
                    "log_url": f"/validate/{job_id}/log",
                    "log_path": record.get("log_path"),
                    "submission_path": record.get("submission_path"),
                }
                for job_id, record in jobs.items()
                if str(record.get("task_id") or "") == task_id
            ]
        by_task = (submissions_by_task_dir / _safe_name(task_id)).resolve()
        return {
            "task_id": task_id,
            "jobs": matches,
            "submission_index_dir": str(by_task) if by_task.exists() else None,
        }

    return app


def run_api(host: Optional[str] = None, port: Optional[int] = None) -> None:
    _configure_logging()
    host = host or os.getenv("ALPHACORE_VALIDATION_HTTP_HOST", "127.0.0.1")
    port = int(port or os.getenv("ALPHACORE_VALIDATION_HTTP_PORT", "8888"))
    app = create_app()
    logger.info("starting validation api host=%s port=%s", host, port)
    uvicorn_run(app, host=host, port=port, log_level=os.getenv("ALPHACORE_VALIDATION_UVICORN_LOG_LEVEL", "info"))


if __name__ == "__main__":
    run_api()
