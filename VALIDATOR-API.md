# Validation API

The Validation API is a small HTTP service that runs **sandboxed task validation**. It executes untrusted miner submissions inside Firecracker microVMs and returns a score.

Today the sandbox is primarily used for Terraform-based tasks on GCP, but it’s designed to expand to other clouds, decentralized providers, and non-Terraform validation workflows over time.

This service is separate from the validator neuron:
- The **validator** detects miners and uses the task generation pipeline to send them tasks.
- The **Validation API** runs the sandbox (Terraform + network policy + artifact capture).

## Scoring (0..1 from invariants)

Inside the microVM, the validator script checks the task’s invariant list against `terraform.tfstate` and emits:

- `result.score` in `[0.0, 1.0]` computed as `passed_invariants / total_invariants`
- `result.status`:
  - `pass` only when **all** invariants pass (`score == 1.0`)
  - `fail` otherwise

The `result` payload usually includes `passed_invariants` and `total_invariants` for debugging.

## One-time host provisioning (required for Firecracker jobs)

Tested host OSe
- Ubuntu 22.04
- Debian 12

On the machine that will run the Validation API:

```bash
sudo bash modules/evaluation/validation/sandbox/check-kvm.sh
sudo bash modules/evaluation/validation/sandbox/setup.sh
```

`setup.sh` installs and configures the sandbox runtime, including:
- Firecracker binaries + a pinned kernel/rootfs image.
- A curated Terraform provider mirror + a `terraform.rc` that disables direct registry downloads.
- A bridge + TAP pool for microVM networking plus an egress policy:
  - `dnsmasq` provides DHCP + a DNS allowlist.
  - `tinyproxy` enforces an HTTP(S) allowlist.
  - `iptables` drops other forwarded traffic and blocks the metadata server.

The pinned versions live at the top of `modules/evaluation/validation/sandbox/setup.sh` (Terraform and provider versions are intentionally locked).

## Secure-by-default validation architecture

The sandbox is designed so the validator can execute untrusted miner submissions with strong isolation and strict egress controls:

- **Firecracker microVM + jailer**: each validation runs in an isolated microVM with a jailed filesystem.
- **Read-only base image**: the rootfs is mounted read-only; only a dedicated results volume is writable.
- **Defensive zip handling**: miner zip extraction rejects path traversal and symlinks and enforces size/count limits before anything runs.
- **Workspace sanitization**: the sandbox removes `.terraform/`, `.terraform.lock.hcl`, and non-infra files so miners can’t force arbitrary provider pins or smuggle extra executables.
- **Provider mirror enforcement**: Terraform is configured with a `terraform.rc` that blocks direct provider downloads and forces a filesystem mirror baked into the rootfs.
- **Restricted networking**: the guest network is isolated behind a bridge/TAP pool with a deny-by-default policy; DNS and HTTP(S) egress are constrained and the metadata server is blocked.
- **Token redaction**: sandbox logs and error payloads redact the access token when present.

## Credentials (today: GCP)

The Validation API can mint short-lived OAuth access tokens from a service account JSON key and pass them into the sandbox.

Important: the sandbox injects **only the short-lived access token** (as `GOOGLE_OAUTH_ACCESS_TOKEN`) into the microVM — it does **not** mount or copy the service account key into the guest.

Best practice:
- The validator’s GCP credentials should be dedicated to validation only.
- Grant the minimum permissions needed to verify miner submissions in your validation project, and avoid granting unrelated roles.

As new clouds/providers are added, this section will expand with equivalent “mint a short-lived token; inject only the token” patterns.

You have two options:
- Pass a key file via `--gcp-creds-file` (recommended for deployments).
- Or set `GOOGLE_OAUTH_ACCESS_TOKEN` (useful for local testing; not written to disk).

## Launch (PM2)

```bash
bash scripts/validator/process/launch_validation_api.sh \
  --network test \
  --gcp-creds-file <path-to-gcp-creds.json>
```

Defaults (from the launcher/env):
- Bind: `127.0.0.1:8888`
- venv: `.venv-validation-api/`
- env file: `env/<network>/validation-api.env`
- PM2 process: `alphacore-validation-api-<network>`

Health check:

```bash
curl -sS http://127.0.0.1:8888/health
```

Logs:

```bash
pm2 logs alphacore-validation-api-test
```

## API endpoints (for debugging)

The service exposes:
- `GET /health`
  - Returns readiness and queue state (`sandbox_ready`, `token_ready`, queue size, workers).
- `POST /validate`
  - Request JSON:
    - `workspace_zip_path` (string, must end in `.zip`, must exist on the validator host)
    - `task_json` (object)
    - `timeout_s` (int, default `120`)
    - `net_checks` (bool, default `false`)
    - `stream_log` (bool, default `false`)
    - `quiet_kernel` (bool, default `true`)
  - Response JSON:
    - `job_id`, `task_id`, `result`, `log_url`, `log_path`, `submission_path`, `tap`
  - Notes:
    - This call queues the job and then waits for completion.
    - When the queue is full it returns `429` with `Retry-After: 1`.
    - When the sandbox/token manager isn’t ready it returns `503`.
- `GET /validate/active`
  - Returns jobs that are `queued` or `running`.
- `GET /validate/{job_id}`
  - Returns the stored job record (`status`, timestamps, `result`, `log_path`, `log_tail`).
- `GET /validate/{job_id}/log?tail=200`
  - Returns the tail of the job log (plain text).
- `GET /task/{task_id}`
  - Returns all recorded jobs for a given `task_id` and where the stored submissions are indexed.

For an end-to-end request example, use the included test helpers:

```bash
python3 modules/evaluation/validation/sandbox/test_zip/make_zip.py
python3 modules/evaluation/validation/sandbox/test_zip/submit_validate.py --api-url http://127.0.0.1:8888
```

## Notes / knobs

- If set, `ALPHACORE_VALIDATION_ARCHIVE_ROOT` restricts `workspace_zip_path` to a single directory tree.
- The sandbox runner is typically launched via `sudo -n` (configured by `setup.sh`) because it needs root for mounts/jailer setup.
- Log retention is configured by `setup.sh` via logrotate (`/etc/logrotate.d/alphacore-logs`) and systemd-tmpfiles (`/etc/tmpfiles.d/alphacore-logs.conf`).
  - Default: PM2 logs rotate daily or at 100MB, keep 30 days.
  - Default: validation/sandbox/taskgen logs are cleaned after 30d; round summaries after 180d.
