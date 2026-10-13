import shlex
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, Field

from .git import Repo

# Allow only characters that cannot break out of a shell word, because downstream tests can rewrite the report.
_SHELL_SAFE = r"^[A-Za-z0-9._:-]*$"
Arch = Literal["x86_64", "aarch64"]
Size = Literal["small", "medium", "large", "xlarge", "2xlarge"]


class Environment(BaseModel, frozen=True):
    image: str = Field(pattern=_SHELL_SAFE)
    compiler: str = Field(pattern=_SHELL_SAFE)
    arch: Arch
    size: Size
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
    def arches(self) -> list[Arch]:
        return sorted({env.arch for env in self.environments})

    @property
    def runner(self) -> Path:
        return Path("tests/ci/integration") / f"run_{self.name}_integration.sh"

    # List <name>_patch and every other patch folder the runner mentions, relative to the aws-lc root.
    # Example: tpm2_tss gives tpm2_tools_patch and tpm2_tss_patch.
    def patch_dirs(self, awslc: Path) -> list[Path]:
        runner_text = (awslc / self.runner).read_text()
        named = {folder.name for folder in (awslc / self.runner.parent).glob("*_patch") if folder.name in runner_text}
        return sorted(self.runner.parent / name for name in named | {f"{self.name}_patch"})

    def on(self, arch: str) -> list[Environment]:
        return [env for env in self.environments if env.arch == arch]

    # Pick the largest CodeBuild instance size among the failed CI jobs on one arch, so replays run as fast as CI.
    # Example: mysql jobs on 2xlarge give 2xlarge.
    def size(self, arch: str) -> Size:
        return max((env.size for env in self.on(arch)), key=get_args(Size).index)

    # Build the bash script a failed CI job ran from the aws-lc root, with the aws CLI stubbed out and as the job's user.
    def script(self, env: Environment) -> str:
        setup = f"/opt/compiler-env/setup-{env.compiler}.sh"
        run = f"./{self.runner} {shlex.quote(self.version)}"
        script = f"aws() {{ :; }}; export -f aws; [ -f {setup} ] && source {setup}; {run}"
        if env.user:
            script = (f"mkdir -p /home/{env.user}; chown -R {env.user} /home/{env.user} .. 2>/dev/null; "
                      f"su -p {env.user} -c {shlex.quote(f'export HOME=/home/{env.user}; {script}')}")
        return script
