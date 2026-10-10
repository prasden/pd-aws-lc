import argparse
import json
from pathlib import Path

from .git import GitHubClient
from .harness import Harness

with_target = argparse.ArgumentParser(add_help=False)
with_target.add_argument("--target", required=True, help="Target folder name printed by recognize, e.g. ruby-master.")
with_registry = argparse.ArgumentParser(add_help=False, parents=[with_target])
with_registry.add_argument("--registry", required=True, help="Registry that holds the aws-lc CI images.")

parser = argparse.ArgumentParser(prog="autofix")
parser.add_argument("--work-dir", type=Path, required=True, help="Directory for sandboxes, logs, clones, and results.")
commands = parser.add_subparsers(dest="command", required=True)
recognize = commands.add_parser("recognize", help="Collect the failed targets, logs, and repos of an omnibus run.")
recognize.add_argument("--repo", required=True, help="Repository of the omnibus run, e.g. aws/aws-lc.")
recognize.add_argument("--run-id", type=int, required=True, help="ID of the omnibus run that failed.")
commands.add_parser("reason", parents=[with_registry], help="Repair one target with the agent, then review the fix.")
commands.add_parser("verify", parents=[with_registry], help="Replay the target's CI jobs on a clean clone with the fix.")
resolve = commands.add_parser("resolve", parents=[with_target], help="Push a verified fix to the fork and open a draft PR.")
resolve.add_argument("--repo", required=True, help="Repository to open the PR against, e.g. aws/aws-lc.")
resolve.add_argument("--fork", required=True, help="Fork to push the branch to, e.g. prasden/pd-aws-lc.")

args = parser.parse_args()
harness = Harness(args.work_dir)
match args.command:
    case "recognize":
        print(json.dumps(harness.recognize(GitHubClient(args.repo), args.run_id)))
    case "reason":
        print(harness.reason(harness.load(args.target), args.registry).model_dump_json())
    case "verify":
        raise SystemExit(not harness.verify(harness.load(args.target), args.registry).passed)
    case "resolve":
        print(harness.resolve(harness.load(args.target), GitHubClient(args.repo), args.fork))
