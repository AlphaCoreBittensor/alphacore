# Miner

This repo includes a **starter miner** (`neurons/miner.py`). It is intentionally minimal: it logs synapse traffic, returns a placeholder `not_implemented` result, and (optionally) attaches a tiny example ZIP so you can validate end-to-end transport.

Contributors are expected to implement their own miner logic and point the launcher at it.

Today tasks largely target Terraform on GCP, but miners should expect additional clouds, decentralized providers, and task types beyond Terraform generation over time.

## Synapses

AlphaCore miners expose a small set of Bittensor synapses (see `subnet/protocol.py` and the starter implementation in `neurons/miner.py`):

- `StartRoundSynapse` — round handshake / liveness check from the validator.
  - Miner sets: `is_ready`, `available_capacity`, `miner_version`, `error_message`.
- `TaskSynapse` — the actual task request.
  - Validator sends: `task_id` + natural-language `prompt` (prompt-only; no invariants).
  - Miner responds: `result_summary` (an `ACResult` dict), optional `evidence_hint`, and optionally `workspace_zip_b64` (base64 ZIP + metadata).
- `TaskFeedbackSynapse` — feedback after evaluation.
  - Validator sends: `score` (`0..1`), `latency_seconds`, optional `feedback_text` / `suggestions`.
  - Miner responds: `acknowledged=true`.
- `TaskCleanupSynapse` — post-validation cleanup hook.
  - Validator sends: `validation_response` (score + log URLs/paths).
  - Miner responds: `acknowledged=true`, `cleanup_ok=true|false`, optional `error_message`.

The base miner also runs per-synapse `blacklist_*` and `priority_*` hooks (e.g. minimum stake / validator permit checks). These are configurable via miner launch flags like `--min-stake`, `--no-force-validator-permit`, and `--allow-non-registered`.

## Launch (PM2)

```bash
bash scripts/miner/process/launch_miner.sh \
  --network <network.name> \
  --netuid <netuid> \
  --wallet-name <wallet.name> \
  --hotkey <wallet.hotkey> \
  --wallet-path "$HOME/.bittensor/wallets" \
  --axon-port 8091 \
  --external-ip <public_ip>
```

Process naming:
- By default the PM2 process name is `${WALLET_HOTKEY}-netuid${NETUID}`.

Logs:

```bash
pm2 logs miner-test-netuid123
```

## Example launch

Point the launcher at your file:

```bash
bash scripts/miner/process/launch_miner.sh \
  --network <network.name> \
  --netuid <netuid> \
  --wallet-name <wallet.name> \
  --hotkey <wallet.hotkey> \
  --wallet-path "$HOME/.bittensor/wallets" \
  --axon-port 8091 \
  --external-ip <public_ip> \
  --entrypoint miner.py
```

For runs on testnet:
```bash
  bash scripts/miner/process/launch_miner.sh \
    --network test --netuid 348 \
    --wallet-name <wallet.name> --hotkey <wallet.hotkey> --wallet-path "$HOME/.bittensor/wallets" \
    --axon-port 8091 --external-ip <external_ip> \
    --no-force-validator-permit \
    --min-stake 0 \
    --allow-non-registered
```

The PM2 wrapper:
- sources the generated env file under `env/<network>/`
- activates `venv/`
- sets `PYTHONPATH` to the repo root
- runs `python <entrypoint>`

## Logging levels

To increase bittensor logging verbosity, pass:

```bash
--bt-logging-level debug
```

or:

```bash
--bt-logging-level trace
```

## Safety notes

If your miner provisions real infrastructure (or uses cloud credentials), you are responsible for operating safely:
- Use a dedicated cloud project/account and least-privilege IAM
- Prefer short-lived credential access
- When granting access for verification, prefer read-only roles and scope them narrowly

## Validation-side provider mirror (today: Terraform/GCP)

Validation happens inside a Firecracker microVM with a **filesystem provider mirror** baked into the rootfs. Direct downloads from `registry.terraform.io` are disabled, so your submission must be compatible with the pinned Terraform/provider versions on the validator.

Pinned versions are defined in `modules/evaluation/validation/sandbox/setup.sh`:

- Terraform: `1.14.0`
- `hashicorp/google`: `7.12.0`
- `hashicorp/google-beta`: `7.12.0`
- `hashicorp/random`: `3.7.2`

Practical implications for miners:
- Include `required_version` and `required_providers` constraints that match the pinned versions, otherwise `terraform init` inside the sandbox can fail because the mirror won’t have the version Terraform tries to select.
- Avoid using providers that aren’t mirrored (they won’t be downloadable during validation).
