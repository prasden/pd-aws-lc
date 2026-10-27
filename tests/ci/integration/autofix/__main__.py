import json
import os
import sys
from pathlib import Path

from . import harness
from .git import Git, Repo
from .target import IntegrationTarget


def _harness() -> harness.Harness:
    return harness.Harness(Path(os.environ["WORK_DIR"]), IntegrationTarget.model_validate_json(os.environ["TARGET"]))


match sys.argv[1:]:
    case ["recognize", reports]:
        texts = [report.read_text() for report in Path(reports).rglob("integration-failure.txt")]
        print(json.dumps(harness.recognize(texts, Repo(f"https://github.com/{os.environ['GITHUB_REPOSITORY']}.git",
                                                       os.environ["AWSLC_SHA"]))))
    case ["reason"]:
        sys.exit(not _harness().reason(os.environ["ECR_REGISTRY"]).passed)
    case ["review"]:
        sys.exit(not _harness().review().safe)
    case ["resolve", checkout]:
        pr = _harness().resolve(Git(Path(checkout)), os.environ["RUN_URL"])
        print("\n".join(f"{key}={value}" for key, value in pr.items()))
    case _:
        sys.exit("usage: python -m autofix recognize REPORTS | reason | review | resolve CHECKOUT")
