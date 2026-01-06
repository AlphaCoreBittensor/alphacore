# Auto-update (origin/release)

This repo includes a safe auto-updater intended for validator machines running the Validation API + validator under PM2.

## What it does

- Polls `origin/release` (configurable) and applies updates when available.
- Defers updates while:
  - the validator is mid-round (lockfile written by the validator), or
  - the Validation API is busy running sandbox jobs (`GET /validate/active`).
- Stops and re-launches only the AlphaCore PM2 namespace (`alphacore`).
- Restarts Validation API first, then the validator (validator can fail-fast if the API is unhealthy).

## Files

- Updater: `scripts/validator/process/autoupdate_release.sh`
- Example config: `scripts/validator/process/autoupdate.env.example`
- Validator round lockfile: set by `neurons/validator.py` via `ALPHACORE_ROUND_LOCKFILE` (default: `/tmp/alphacore-validator-round.lock`)

## Default behavior (launch_validator.sh)

`launch_validator.sh` enables auto-updates by default. When it runs, it:

- Writes a per-user config to `~/.config/alphacore/autoupdate/alphacore.env`
- Installs a scheduler via `scripts/validator/process/ensure_autoupdate_timer.sh`

Disable with `--no-autoupdate`, or change the interval with `--autoupdate-interval` / `ALPHACORE_AUTOUPDATE_INTERVAL`.

## One-time machine setup

1. Create and start your processes normally (so PM2 is installed and working).
2. Ensure PM2 startup is configured for the validator user so processes come back after reboot.
   - `modules/evaluation/validation/sandbox/setup.sh` installs PM2 and runs `pm2 startup` automatically.
3. Create a machine-local updater config, e.g.:
   - `sudo install -m 600 scripts/validator/process/autoupdate.env.example /etc/alphacore/autoupdate.env`
   - Edit `/etc/alphacore/autoupdate.env`
4. Ensure the machine can `git fetch` the repo (SSH deploy key or equivalent).

Notes:
- The validator launcher requires `--validator-sa`, but it can be inferred from `--gcp-creds-file` if the JSON includes `client_email` (this is how the example config works).
- If your validator uses LLM prompts, ensure `OPENAI_API_KEY`/`ALPHACORE_OPENAI_API_KEY` is available to the updater environment, or set `VALIDATOR_EXTRA_ARGS="--disable-llm"`. When you launch with an OpenAI key, `launch_validator.sh` writes `ALPHACORE_OPENAI_API_KEY` into the auto-update config (chmod 600).
- `launch_validator.sh` enables an auto-update scheduler by default. Disable with `--no-autoupdate`.
- The updater refuses to run if the working tree is dirty. Use a clean deploy clone for auto-updates.

## Manual run

`bash scripts/validator/process/autoupdate_release.sh --config /etc/alphacore/autoupdate.env`

## Testing

- Dry-run (no restarts): `bash scripts/validator/process/autoupdate_release.sh --config /path/to/autoupdate.env --dry-run`
- To simulate “round active” deferral: `touch /tmp/alphacore-validator-round.lock` then re-run the updater.
- To validate end-to-end:
  1) Create/advance `origin/release` to a new commit.
  2) Ensure validator is idle (no lockfile) and Validation API is idle (`/validate/active` empty).
  3) Run the updater manually once and verify processes restart in the correct PM2 namespace.

## Timer (suggested)

Run the updater every 1–5 minutes via systemd timer or cron. The script uses `flock` so overlapping runs are safe.

If a systemd user timer is used, it will only run on boot after the user logs in unless lingering is enabled:
`sudo loginctl enable-linger <user>`

## Logs

- systemd user timer: `~/.local/state/alphacore/autoupdate/alphacore.log`
- cron fallback: the same log file is used

## Rollback

With a deployment branch, rollback is just moving `origin/release` back to a known-good commit:

- Fast (rewrites release pointer): `git reset --hard <good_sha>` then `git push --force-with-lease origin release`
- Safe history: `git revert <bad_sha>` then `git push origin release`
