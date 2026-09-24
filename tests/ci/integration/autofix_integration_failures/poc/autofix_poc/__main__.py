import os
from typing import Annotated

import typer

from . import fix, ingest
from .core import Target, start_heartbeat, step

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
Version = Annotated[str, typer.Argument()]


@app.callback()
def _start() -> None:
    """Repair failing AWS-LC integration tests with a Strands agent."""
    start_heartbeat()


def _docker(target: Target, log_name: str = "") -> None:
    code, log = target.run_in_docker()
    if log_name:
        (target.dir("logs") / log_name).write_text(log)
    print(f"{log[-6000:]}\n\nrunner exit={code} — {'GREEN' if code == 0 else 'still failing'} (image {target.image})")
    raise typer.Exit(code)


@app.command()
def prep(integration: str, version: Version = "") -> None:
    """Build integration-failure.txt from the runner."""
    ingest.prep(integration, version)


@app.command("ingest")
def ingest_run(run_id: str, repo: str = "aws/aws-lc", integration: str | None = None) -> None:
    """Pull the failure artifact and logs from a GitHub run."""
    ingest.ingest(run_id, repo, integration)


@app.command()
def pr(number: str, repo: str = "aws/aws-lc", open_pr: bool = False) -> None:
    """Detect failing integrations on a PR and autofix them."""
    step(f"finding failing integrations on {repo}#{number}")
    for target in ingest.discover_pr(number, repo):
        print(f"\n=== autofix: {target.name} (detected from PR #{number}) ===")
        fix.autofix(target, open_pr)


@app.command()
def run(integration: str, version: Version = "") -> None:
    """Reproduce the failure by running the runner in Docker."""
    _docker(Target(integration, version), "runner.log")


@app.command()
def reason() -> None:
    """Run the Strands repair and reviewer agents."""
    fix.repair(Target.load(), verify=os.environ.get("AUTOFIX_VERIFY") == "1")


@app.command()
def verify() -> None:
    """Re-run the real runner in Docker to confirm green."""
    _docker(Target.load())


@app.command()
def open_pr(yes: Annotated[bool, typer.Option("--yes")] = False) -> None:
    """Show the draft PR plan. Push and open it with --yes."""
    fix.open_pr(Target.load(), push=yes)


@app.command()
def omnibus(targets: list[str], open_pr: bool = False) -> None:
    """Run each integration[:version] and autofix the failures."""
    for spec in targets:
        target = Target(*spec.split(":", 1))
        print(f"\n=== omnibus: running {target.name} (image {target.image}) ===")
        target = ingest.prep(target.integration, target.version)
        step(f"{target.name}: running runner")
        code, log = target.run_in_docker()
        if code == 0:
            print(f"{target.name}: PASSED — no autofix needed")
            continue
        (target.dir("logs") / "runner.log").write_text(log)
        print(f"{target.name}: FAILED (exit {code}) — triggering autofix")
        fix.autofix(target, open_pr)


if __name__ == "__main__":
    app()
