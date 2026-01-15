# AlphaCore Validator – Quick Start

This guide walks you through setting up and running an **AlphaCore validator** on the Bittensor network.

---

## Overview

Validators are responsible for:
- Generating structured infrastructure tasks
- Dispatching tasks to miners
- Verifying miner submissions against cloud provider APIs
- Scoring results inside secure Firecracker microVM sandboxes

---

## System Requirements

### Minimum Hardware
- **CPU:** 4 vCPUs
- **Memory:** 8 GB RAM
- **Disk:** 128 GB
- **GPU:** Not required

### Operating System
- **Ubuntu 22.04 LTS**

### Virtualization
- **KVM must be enabled**
  - Bare metal: enabled by default
  - Cloud providers: **nested virtualization required**

---

## 1. VM Preparation

### Create the VM
Provision an Ubuntu 22.04 VM that supports KVM (nested virtualization if applicable).

### Clone the Repository
```bash
git clone https://github.com/AlphaCoreBittensor/alphacore.git
cd alphacore
git checkout release
```

### Verify KVM Support
```bash
sudo bash modules/evaluation/validation/sandbox/check-kvm.sh
```

### Install Sandbox Dependencies
```bash
sudo bash modules/evaluation/validation/sandbox/setup.sh
```

---

## 2. Google Cloud Platform (GCP) Setup

Validators use **read-only** access to verify deployed infrastructure.

Billing is required because validators must query GCP APIs as part of the verification process. The validator service account is intentionally permissionless and functions only as an identity shell inside the validator environment. It cannot perform any actions on its own.

Miners are responsible for granting **narrowly scoped read-only** permissions to their deployed resources so validators can verify state. All risk is contained on the miner side through explicit access grants.

### Create GCP Resources
1. Create a **GCP account**
2. Create a **GCP project**
3. Enable billing
   > Billing is required to access GCP APIs (see above)

### Create a Service Account
- Create a service account
- Generate a **JSON key**
- **Do not assign permissions**
  - Validators only pass the service account **email** to miners
  - Miners grant read-only access so validators can verify deployed resources

### Add Credentials to Repo
Place the key at the repository root:
```text
gcp-creds.json
```

### Enable Required APIs
```bash
gcloud services enable \
  serviceusage.googleapis.com \
  artifactregistry.googleapis.com \
  cloudresourcemanager.googleapis.com \
  cloudscheduler.googleapis.com \
  compute.googleapis.com \
  dns.googleapis.com \
  iam.googleapis.com \
  logging.googleapis.com \
  pubsub.googleapis.com \
  secretmanager.googleapis.com \
  storage.googleapis.com \
  --project <project-id>
```

---

## 3. OpenAI Setup

An OpenAI API key is required for **prompt generation**.

- Create an OpenAI API key
- Expected cost: **< $0.25 USD / day**

---

## 4. Start the Validator Services

All commands below should be run **from the repository root**.

### Start the Validation API

This launches the Firecracker-based validation service.

```bash
bash scripts/validator/process/launch_validation_api.sh \
  --network finney \
  --gcp-creds-file /path/to/alphacore/gcp-creds.json
```

### Start the Validator

```bash
bash scripts/validator/process/launch_validator.sh \
  --wallet-name <wallet-name> \
  --hotkey <hotkey-name> \
  --network finney \
  --netuid 66 \
  --gcp-creds-file /path/to/alphacore/gcp-creds.json \
  --timed --tick-seconds 1800 \
  --openai-api-key <openai-api-key>
```

---

## Notes

- Validators **do not deploy infrastructure themselves**
- Verification is performed using:
  - Terraform state
  - Cloud provider read-only APIs
  - Firecracker microVM sandbox execution
- Keep `gcp-creds.json` secure and **never commit it**
