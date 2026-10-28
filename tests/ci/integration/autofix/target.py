import logging
import shlex
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .git import Repo

# Allow only characters that cannot break out of a shell word, because downstream tests can rewrite the report.
_SHELL_SAFE = r"^[A-Za-z0-9._:-]*$"


class Environment(BaseModel, frozen=True):
    image: str = Field(pattern=_SHELL_SAFE)
    compiler: str = Field(pattern=_SHELL_SAFE)
    arch: Literal["x86_64", "aarch64"]
    job_id: int = 0
    user: str = Field("", pattern=_SHELL_SAFE)
    ipv6: bool = False
    env_vars: dict[str, str] = {}


class IntegrationTarget(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9_]+$")
    version: str = Field(pattern=r"^[A-Za-z0-9._/-]*$")
    awslc: Repo
    repos: list[Repo]
    environments: list[Environment]

    @property
    def dir_name(self) -> str:
        return f"{self.name}-{self.version}".rstrip("-").replace("/", "-")

    @property
    def runner(self) -> Path:
        return Path("tests/ci/integration") / f"run_{self.name}_integration.sh"

    def patch_dirs(self, awslc: Path) -> list[Path]:
        """Return the target's own patch folder and every other patch folder its runner script names."""
        runner_text = (awslc / self.runner).read_text()
        named = {folder.name for folder in (awslc / self.runner.parent).glob("*_patch") if folder.name in runner_text}
        return sorted(self.runner.parent / name for name in named | {f"{self.name}_patch"})

    def on(self, arch: str) -> list[Environment]:
        return [env for env in self.environments if env.arch == arch]

    def script(self, env: Environment) -> str:
        """Return the bash script that runs the runner the way the failed CI job did, with the aws CLI stubbed out."""
        setup = f"/opt/compiler-env/setup-{env.compiler}.sh"
        run = f"./{self.runner} {shlex.quote(self.version)}"
        script = f"aws() {{ :; }}; export -f aws; [ -f {setup} ] && source {setup}; {run}"
        if env.user:
            script = (f"mkdir -p /home/{env.user}; chown -R {env.user} /home/{env.user} . 2>/dev/null; "
                      f"su -p {env.user} -c {shlex.quote(script)}")
        return script


def recognize(reports: Path, awslc: Repo) -> list[dict]:
    """Read every failure report in a folder and list the targets to fix, one per integration and version.

    Each failed CI job writes its own report. When python 3.13 fails on both x86_64 and aarch64, the two reports
    become one target, and its reason job runs on x86_64. Reports that do not parse are logged and skipped.
    """
    targets: dict[tuple[str, str], IntegrationTarget] = {}
    for report in sorted(path.read_text() for path in reports.rglob("integration-failure.txt")):
        try:
            head, *lines = report.splitlines()
            name, version = head.split("\t")
            repos = [Repo(*line.removeprefix("commit=").split()) for line in lines if line.startswith("commit=")]
            env = Environment.model_validate_json(lines[-1].removeprefix("job="))
            target = targets.setdefault((name, version), IntegrationTarget(
                name=name, version=version, awslc=awslc, repos=repos, environments=[]))
        except (ValueError, IndexError) as error:
            logging.warning("skipping a bad integration-failure.txt: %s", error)
            continue
        target.environments.append(env)
    return [{"name": target.dir_name, "arch": "x86_64" if target.on("x86_64") else "aarch64",
             "target": target.model_dump(mode="json")} for target in targets.values()]
