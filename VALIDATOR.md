# Validator

This doc covers how to run the **validator neuron**. In typical deployments the validator uses the sandboxed Validation API for scoring; see [`VALIDATOR-API.md`](VALIDATOR-API.md) for the sandbox setup.

Today most tasks are “Terraform on GCP”, but the validator architecture is intended to support additional clouds, decentralized providers, and task types beyond Terraform generation as the protocol evolves.

## What are “invariants”?

An **invariant** is a machine-checkable requirement attached to each task. In AlphaCore tasks, invariants describe what must be true about the miner’s submission output (today: the resulting `terraform.tfstate`) — for example: “a resource with a specific name exists”, “a network rule allows tcp:80”, “an access binding grants viewer to the validator identity”, etc.

During validation:
- The miner submission (a ZIP) is executed and/or inspected in the sandbox, producing a `terraform.tfstate`.
- The validator checks each invariant against that state and marks it pass/fail.
- The sandbox returns a `score` in `[0..1]` based on how many invariants passed (and `pass` requires all invariants to pass).

## Task generation and prompts

Validators generate tasks from a structured spec (provider + resources + invariants) and then produce the miner-facing promp as natural language.

- The task spec stores the invariants and other machine-checkable requirements (what the validator will score).
- The miner receives a prompt-only view of the task (natural language).
- Prompt generation uses an LLM to introduce natural-language variability across runs while keeping the invariant details intact.

By default, this repo is configured to use an LLM model of `gpt-5-mini` for prompt generation (see `modules/generation/instructions.py` and `modules/task_config.yaml`).

Controls:
- `ALPHACORE_ENABLE_LLM=true|false` (validator launch scripts default to enabled)
- `ALPHACORE_TASK_PROMPT_MODEL` (defaults to `gpt-5-mini`)

## Scoring summary

At a high level, the validator computes per-miner scores like this:

- **Per task**: the miner must include a submission ZIP (`workspace_zip_b64`). The validator submits it to the Validation API along with the task JSON (including invariants). The API returns a `score` in `[0..1]` (invariants passed / total).
- **Per miner (per round)**: the validator averages task scores across all tasks evaluated for that miner (default is one task per round/epoch).
- **Default cadence (epoch mode)**: each validator starts at most one round per epoch by default, and the default round size is one task — effectively **one task per epoch per validator** unless you override the loop/task settings.
- **Optional latency mixing**: if enabled, the validator combines the API score with a relative latency score using weights (`ALPHACORE_API_SCORE_WEIGHT`, `ALPHACORE_LATENCY_SCORE_WEIGHT`). If the API score is `0`, latency is ignored and the final score remains `0` (fail-closed).


## Incentives

After scoring, the validator runs a settlement step that determines on-chain weights for the round:

- **Winner-take-all among active miners**: the winner is the highest-scoring UID among the miners the validator considered “active” that round. If no active miner has a positive score, the validator skips setting weights.


## Prerequisites

- On-chain: your hotkey must already be registered on the subnet (`netuid`) you plan to validate on. By default the launcher skips registration; you can opt in with `--register`.
- Operational: `pm2` is used by the launch scripts for supervision (see `scripts/validator/main/install_pm2.sh`).
- If using sandbox scoring: the Validation API should be running and reachable (see `VALIDATOR-API.md`).

## What is KVM?

KVM (Kernel-based Virtual Machine) is the Linux kernel’s hardware-virtualization subsystem. Firecracker uses KVM to run microVMs, so the Validation API host needs `/dev/kvm` available. On cloud VMs this typically means enabling **nested virtualization**.

## One-time setup

If you launch via `scripts/validator/process/launch_validator.sh`, it will bootstrap the validator `venv/` automatically (via `scripts/validator/process/launch_pm2.sh`) if it doesn’t exist.

If you prefer an explicit, interactive setup step, `scripts/validator/main/setup.sh` can also create `venv/` and install Python dependencies into it.

```bash
bash scripts/validator/main/install_dependencies.sh
bash scripts/validator/main/setup.sh
```

## Launch the validator (PM2)

The canonical launcher is `scripts/validator/process/launch_validator.sh`. It writes an env file under `env/<network>/` and starts the validator under PM2.

Required inputs:
- wallet name + hotkey
- netuid + network
- validator service account email (embedded in generated tasks). Provide it via `--validator-sa` or pass `--gcp-creds-file` and the launcher will infer it from `client_email` in the JSON.

Minimum example:

```bash
bash scripts/validator/process/launch_validator.sh \
  --wallet-name <wallet.name> \
  --hotkey <wallet.hotkey> \
  --netuid <netuid> \
  --network <network> \
  --gcp-creds-file <path-to-gcp-creds.json>
```

Logs:

```bash
pm2 logs validator-<wallet.hotkey>-<network>
```

## If you see “venv not found”

That error comes from the PM2 start wrapper (`scripts/validator/process/start_validator_pm2.sh`) when the process is started without a prepared `venv/` (commonly if you ran `pm2 start scripts/validator/process/pm2.config.js` directly).

Fix options:
- Use `bash scripts/validator/process/launch_validator.sh ...` (recommended; it bootstraps the venv before starting PM2).
- Or run `bash scripts/validator/main/setup.sh` once to create `venv/`.

## Epoch mode vs timed mode

The validator supports two loop modes:

- **Epoch mode (default)**: rounds are gated by chain epoch progress. By default the validator starts at most one round per epoch and can optionally “slot” round-starts across the epoch to avoid multiple validators starting at the same time (`--epoch-slots`, `--epoch-slot-index`).
- **Timed mode**: ignores epoch gating and runs on a fixed tick interval (useful for debugging and fast iteration).

To use timed mode:

```bash
bash scripts/validator/process/launch_validator.sh \
  --wallet-name <wallet.name> \
  --hotkey <wallet.hotkey> \
  --netuid <netuid> \
  --network <network> \
  --gcp-creds-file <path-to-gcp-creds.json> \
  --timed \
  --tick-seconds 30
```

## Launch validator + Validation API together

If you want a single command that starts both services, use:

```bash
bash scripts/validator/process/launch_with_validation_api.sh \
  --wallet-name <wallet.name> \
  --hotkey <wallet.hotkey> \
  --netuid <netuid> \
  --network <network> \
  --gcp-creds-file <path-to-gcp-creds.json>
```

## Auto-update (origin/release)

The validator launcher installs a per-user auto-update timer by default and writes a config under `~/.config/alphacore/autoupdate/`. See `scripts/validator/process/AUTOUPDATE.md` for setup, gating behavior, and testing.
