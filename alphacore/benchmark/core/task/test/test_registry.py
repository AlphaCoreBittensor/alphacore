from alphacore.benchmark.core.task.terraform.registry import terraform_task_registry

# Randomly select a GCP task builder based on actual files
task_name, builder = terraform_task_registry.pick_random_task("gcp")
task = builder(validator_sa="alpha-core-validator@alph-478521.iam.gserviceaccount.com")
print(f"Selected task: {task_name}")
print(task.to_json(indent=2))
