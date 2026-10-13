import json
from pathlib import Path
from typing import Annotated

import typer

from .git import Git, Repo
from . import harness
from .harness import Harness
from .sandbox import Sandbox
from .target import IntegrationTarget

WorkDir = Annotated[Path, typer.Option(envvar="WORK_DIR", help="Directory for clones and results.")]
Target = Annotated[IntegrationTarget, typer.Option(envvar="TARGET", parser=IntegrationTarget.model_validate_json,
                                                   help="Target JSON from the recognize matrix.")]
Registry = Annotated[str, typer.Option(envvar="ECR_REGISTRY", help="Registry that holds the aws-lc CI images.")]

app = typer.Typer(pretty_exceptions_enable=False)


@app.command()
def recognize(reports: Annotated[Path, typer.Option(help="Directory of downloaded integration-failure artifacts.")],
              awslc_repo: Annotated[str, typer.Option(help="aws-lc clone URL, e.g. https://github.com/aws/aws-lc.git.")],
              awslc_sha: Annotated[str, typer.Option(help="aws-lc commit the omnibus run tested.")]) -> None:
    """Print the job matrix for the failed targets of an omnibus run."""
    texts = [report.read_text() for report in reports.rglob("integration-failure.txt")]
    print(json.dumps(harness.recognize(texts, Repo(awslc_repo, awslc_sha))))


@app.command()
def reason(work_dir: WorkDir, target: Target, registry: Registry) -> None:
    """Repair the target with the agent and verify the fix."""
    raise typer.Exit(not Harness(work_dir, target).reason(registry).passed)


@app.command()
def verify(work_dir: WorkDir, target: Target, registry: Registry) -> None:
    """Replay this runner's arch of failed CI jobs with the fix."""
    raise typer.Exit(not Harness(work_dir, target).verify(Sandbox(work_dir, target, registry)).passed)


@app.command()
def review(work_dir: WorkDir, target: Target) -> None:
    """Judge the verified fix with the rules and the reviewer."""
    raise typer.Exit(not Harness(work_dir, target).review().safe)


@app.command()
def resolve(work_dir: WorkDir, target: Target,
            checkout: Annotated[Path, typer.Option(help="Full aws-lc checkout at the tested commit.")]) -> None:
    """Commit the fix and print its branch and PR title."""
    print(json.dumps(Harness(work_dir, target).resolve(Git(checkout))))


app()
