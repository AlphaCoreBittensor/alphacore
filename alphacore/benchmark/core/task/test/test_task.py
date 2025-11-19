from alphacore.benchmark.core.task.terraform.providers.gcp.vm import build_task

if __name__ == "__main__":
    task = build_task(
        validator_sa="alpha-core-validator@alph-478521.iam.gserviceaccount.com"
    )
    print(task.to_json(indent=2))
