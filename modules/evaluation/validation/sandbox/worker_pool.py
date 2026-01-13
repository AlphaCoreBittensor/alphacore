"""
Async worker pool for running sandbox evaluations concurrently.

This is intended for validator-side integration where responses are queued and a
bounded number of Firecracker runs are executed in parallel.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


SANDBOX_SCRIPT = Path(__file__).with_name("sandbox.py")


@dataclass(frozen=True)
class SandboxJob:
    job_id: str
    workspace_zip: Optional[Path] = None
    workspace_dir: Optional[Path] = None
    task_json: Optional[dict] = None
    creds_file: Optional[Path] = None
    access_token: Optional[str] = None
    stream_log: bool = True
    quiet_kernel: bool = True
    net_checks: bool = False
    timeout_s: int = 120
    log_path: Optional[Path] = None
    extra_args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxJobResult:
    job_id: str
    returncode: int
    summary: dict
    stdout_tail: str
    log_path: Optional[str] = None


class SandboxWorkerPool:
    def __init__(
        self,
        *,
        max_workers: int,
        python: Path = Path(sys.executable),
        use_sudo: bool = True,
        sudo_bin: Path = Path("sudo"),
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self._max_workers = max_workers
        self._python = python
        self._use_sudo = use_sudo
        self._sudo_bin = sudo_bin
        self._sem = asyncio.Semaphore(max_workers)

    async def run_many(self, jobs: list[SandboxJob]) -> list[SandboxJobResult]:
        coros = [self._run_one(job) for job in jobs]
        return await asyncio.gather(*coros)

    async def run_one(self, job: SandboxJob) -> SandboxJobResult:
        return await self._run_one(job)

    async def _run_one(self, job: SandboxJob) -> SandboxJobResult:
        async with self._sem:
            with tempfile.TemporaryDirectory(prefix=f"acore-sbx-{job.job_id}-") as tmp:
                out_json = Path(tmp) / "result.json"
                include_path: Optional[Path] = None
                if job.task_json is not None:
                    include_path = Path(tmp) / "task.json"
                    include_path.write_text(json.dumps(job.task_json), encoding="utf-8")
                stdout_tail, returncode = await self._run_subprocess(job, out_json=out_json, include_path=include_path)
                summary: dict
                if out_json.exists():
                    try:
                        summary = json.loads(out_json.read_text(encoding="utf-8"))
                    except Exception:
                        summary = {"id": job.job_id, "success": False, "error": "failed to parse output json"}
                else:
                    summary = {"id": job.job_id, "success": False, "error": "missing output json"}

                return SandboxJobResult(
                    job_id=job.job_id,
                    returncode=returncode,
                    summary=summary,
                    stdout_tail=stdout_tail,
                    log_path=str(job.log_path) if job.log_path else None,
                )

    async def _run_subprocess(
        self,
        job: SandboxJob,
        *,
        out_json: Path,
        include_path: Optional[Path],
    ) -> tuple[str, int]:
        cmd: list[str] = []
        if self._use_sudo:
            cmd.extend([str(self._sudo_bin), "-n"])
        cmd.extend(
            [
                str(self._python),
                str(SANDBOX_SCRIPT),
                "--timeout",
                str(job.timeout_s),
                "--output-json",
                str(out_json),
            ]
        )
        if job.workspace_zip and job.workspace_dir:
            raise ValueError("Provide only one of workspace_zip or workspace_dir.")
        if job.workspace_zip:
            cmd.extend(["--workspace-zip", str(job.workspace_zip)])
        elif job.workspace_dir:
            cmd.extend(["--workspace-dir", str(job.workspace_dir)])
        else:
            raise ValueError("A sandbox job requires workspace_zip or workspace_dir.")

        if include_path is not None:
            cmd.extend(["--include-path", str(include_path)])
        if job.creds_file:
            cmd.extend(["--creds-file", str(job.creds_file)])
        if job.access_token:
            cmd.extend(["--access-token", job.access_token])
        if job.stream_log:
            cmd.append("--stream-log")
        if job.quiet_kernel:
            cmd.append("--quiet-kernel")
        if job.net_checks:
            cmd.append("--net-checks")
        # Always use deterministic static IPs derived from the allocated tap to avoid DHCP contention
        # under parallel sandbox runs.
        cmd.append("--static-ip-from-tap")
        cmd.extend(job.extra_args)

        env = os.environ.copy()
        env.update(job.env)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        token = job.access_token or job.env.get("GOOGLE_OAUTH_ACCESS_TOKEN")
        tail: deque[str] = deque(maxlen=200)

        log_handle = None
        try:
            if job.log_path is not None:
                job.log_path.parent.mkdir(parents=True, exist_ok=True)
                log_handle = open(job.log_path, "w", encoding="utf-8", errors="replace")
                os.chmod(job.log_path, 0o600)
                cmd_display = [arg if arg != token else "[REDACTED]" for arg in cmd]
                log_handle.write(f"[Host] command: {' '.join(cmd_display)}\n")
                log_handle.flush()

            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                if token:
                    text = text.replace(token, "[REDACTED]")
                if log_handle:
                    log_handle.write(text)
                    log_handle.flush()
                tail.append(text.rstrip("\n"))

            returncode = await proc.wait()
        finally:
            if log_handle:
                log_handle.close()

        return "\n".join(tail), int(returncode or 0)


async def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run multiple sandbox jobs concurrently.")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--workspace-dir", type=Path)
    parser.add_argument("--workspace-zip", type=Path)
    parser.add_argument("--creds-file", type=Path)
    parser.add_argument("--jobs", type=int, default=2, help="Number of identical jobs to run (for load testing).")
    args = parser.parse_args()

    token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
    if not token:
        raise SystemExit("GOOGLE_OAUTH_ACCESS_TOKEN must be set.")

    pool = SandboxWorkerPool(max_workers=args.workers)
    if bool(args.workspace_dir) == bool(args.workspace_zip):
        raise SystemExit("Provide exactly one of --workspace-dir or --workspace-zip.")
    jobs = [
        SandboxJob(
            job_id=f"job-{i}",
            workspace_dir=args.workspace_dir,
            workspace_zip=args.workspace_zip,
            creds_file=args.creds_file,
            env={"GOOGLE_OAUTH_ACCESS_TOKEN": token},
        )
        for i in range(args.jobs)
    ]
    results = await pool.run_many(jobs)
    for res in results:
        print(
            f"[{res.job_id}] success={res.summary.get('success')} score={res.summary.get('score')} "
            f"tap={res.summary.get('tap')} log={res.log_path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
