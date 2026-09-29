import argparse
from pathlib import Path

from .harness import Harness

parser = argparse.ArgumentParser(prog="autofix")
commands = parser.add_subparsers(required=True)

fix_command = commands.add_parser("fix", help="Repair the integrations that failed in an omnibus run.")
fix_command.add_argument("--repo", required=True, help="GitHub repository of the omnibus run, e.g. aws/aws-lc.")
fix_command.add_argument("--run-id", required=True, help="ID of the omnibus run that failed.")
fix_command.add_argument("--work-dir", type=Path, required=True, help="Directory for artifacts, logs, and clones.")
fix_command.set_defaults(run=lambda args: Harness(args.repo, args.run_id, args.work_dir).fix())

args = parser.parse_args()
args.run(args)
