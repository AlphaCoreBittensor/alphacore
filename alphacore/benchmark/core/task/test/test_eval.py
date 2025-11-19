import json
import sys
import os
import logging
from alphacore.benchmark.core.evaluation.evaluator import Evaluator

# Usage: python test_eval.py <task_json> <bundle_dir> [<gcp_service_account_key>]


def main():
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 3:
        print("Usage: python test_eval.py <task_json> <bundle_dir>")
        sys.exit(1)

    task_json = sys.argv[1]
    bundle_dir = sys.argv[2]

    logging.info(f"Loading task from {task_json}")
    with open(task_json, "r") as f:
        task = json.load(f)

    response = {"bundle_dir": bundle_dir}

    logging.info(f"Running evaluator on bundle: {bundle_dir}")
    evaluator = Evaluator()
    score = evaluator.evaluate(task, response)
    logging.info("Evaluation result:")
    print(score)


if __name__ == "__main__":
    main()
