import json
import os
import sys
from pathlib import Path

from .git import Repo
from .harness import Harness
from .target import recognize

match sys.argv[1:]:
    case ["recognize", reports]:
        awslc = Repo(f"https://github.com/{os.environ['GITHUB_REPOSITORY']}.git", os.environ["AWSLC_SHA"])
        print(json.dumps(recognize(Path(reports), awslc)))
    case ["reason"]:
        sys.exit(not Harness.from_env().reason(os.environ["ECR_REGISTRY"]).passed)
    case ["review"]:
        sys.exit(not Harness.from_env().review(os.environ["RUN_URL"]).safe)
    case _:
        sys.exit("usage: python -m autofix recognize REPORTS | reason | review")
