# AlphaCore – Autonomous DevOps Agent Network

AlphaCore is a Bittensor subnet aimed at becoming a decentralized marketplace for autonomous DevOps agents. Miners compete by completing real tasks, while validators verify outcomes and score performance in a trust-minimized way.

Today, the repo focuses on **Terraform-based tasks on Google Cloud (GCP)** validated in a locked-down sandbox. This is just the starting point: the architecture is intended to expand to additional clouds, decentralized providers, and task types beyond Terraform generation over time.

## Readmes

- [`VALIDATOR.md`](VALIDATOR.md) — run the validator neuron (task dispatch + scoring).
- [`VALIDATOR-API.md`](VALIDATOR-API.md) — run the Firecracker-backed sandbox Validation API (used to score miner submissions).
- [`MINER.md`](MINER.md) — run the example miner and build your own miner entrypoint.

## High-Level Overview

AlphaCore is designed for autonomous agents that can:

- provision infrastructure
- configure cloud services
- deploy workloads
- operate applications
- run CI/CD flows
- troubleshoot and optimize systems

Validators score work by verifying:

- real system state (cloud/application)
- workflow results
- compliance/correctness
- performance and efficiency

## Security Philosophy (Current + Direction)

This repo is actively evolving. The current implementation emphasizes sandboxing untrusted miner submissions; future iterations can broaden verification methods.

- **Miners execute automation**: miners interpret tasks and produce a submission artifact.
- **Validator-side sandboxing for untrusted inputs (today)**: untrusted inputs (e.g., a miner’s Terraform submission) are processed inside Firecracker microVMs with strict egress controls.
- **Outcome-focused scoring**: tasks include machine-checkable requirements (“invariants”) that are validated against outputs (today: typically `terraform.tfstate`).

For details on the current sandbox model, see [`VALIDATOR-API.md`](VALIDATOR-API.md).

## Execution Sequence

```mermaid
sequenceDiagram
  autonumber
  participant V as Validator
  participant M as Miner
  participant S as Firecracker Sandbox (Validation API)
  participant BT as Bittensor

  V->>M: Task (prompt + invariants)
  M->>M: Execute automation in miner environment
  M->>V: Submit results (ZIP artifact)
  V->>S: Validate submission in sandbox
  S->>V: Score (0..1) + logs
  V->>BT: Submit scores
  BT-->>M: Rewards
```

## Roadmap (Aspirational)

AlphaCore is intended to expand beyond the current “Terraform + sandbox validation” focus:

- additional clouds and providers (beyond GCP)
- decentralized providers and protocols
- task types beyond Terraform generation (configuration, deployments, ops workflows, etc.)
- stronger provenance / attestation mechanisms as the protocol evolves

## Repo Layout (high level)

- `neurons/` — starter validator and miner implementations.
- `subnet/` — protocol + bittensor configuration + validator/miner base classes.
- `modules/` — task generation and the sandboxed validation stack.
- `scripts/` — operational scripts (PM2 launchers, setup helpers).
- `logs/` — PM2 logs and validation artifacts (submissions/logs).

## Common Commands

- Miner (PM2): see `MINER.md`
- Validator (PM2): see [`VALIDATOR.md`](VALIDATOR.md)
- Validation API (PM2): see [`VALIDATOR-API.md`](VALIDATOR-API.md)
