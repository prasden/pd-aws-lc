import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field
from strands import tool
from strands.types.tools import AgentTool

from .integration import Environment, IntegrationTarget

_AI_CONFIG = json.loads((Path(__file__).parents[4] / ".github/workflows/ai-config.json").read_text())
_RUNNER_TIMEOUT = 3600
_LOG_TAIL = 8000

_CONTAINER_AWSLC = Path("/aws-lc")


def _prompt(name: str) -> str:
    return (Path(__file__).parent / "prompts" / f"{name}.md").read_text()


MODEL = f"bedrock/{_AI_CONFIG['opus']}"
SYSTEM_PROMPT = _prompt("system")
TASK_PROMPT = _prompt("task")
RETRY_PROMPT = _prompt("retry")
REVIEW_PROMPT = _prompt("review")
REVIEW_TASK_PROMPT = _prompt("review_task")


class ReviewVerdict(BaseModel):
    safe: bool = Field(description="true only if the change is a minimal, secret-free fix for the failure")
    findings: list[str] = Field(default_factory=list, description="every suspicious item, quoted exactly")
    rationale: str = Field(description="one-sentence justification")


@dataclass(frozen=True)
class RunResult:
    environment: Environment
    exit_code: int
    log_tail: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    def __str__(self) -> str:
        status = "PASSED" if self.passed else f"FAILED (exit {self.exit_code})"
        return f"{self.environment.arch}/{self.environment.image}: {status}\n{self.log_tail}"


def describe_results(results: list[RunResult]) -> str:
    return "\n\n".join(map(str, results))


def _as_user(user: str, command: str) -> str:
    return (f"mkdir -p /home/{user} && chown -R {user} /home/{user} {_CONTAINER_AWSLC} && "
            f"su -p {user} -c {shlex.quote(command)}")


def _run_environment(target: IntegrationTarget, environment: Environment, awslc: Path, registry: str) -> RunResult:
    command = (f"source /opt/compiler-env/setup-{environment.compiler}.sh && "
               f"{target.runner(_CONTAINER_AWSLC)} {shlex.quote(target.version)}")
    if environment.user:
        command = _as_user(environment.user, command)

    result = subprocess.run([
        "docker", "run", "--rm",
        f"--volume={awslc}:{_CONTAINER_AWSLC}",
        f"--workdir={_CONTAINER_AWSLC}",
        *(f"--env={key}={value}" for key, value in environment.env_vars.items()),
        *(["--sysctl=net.ipv6.conf.all.disable_ipv6=0"] if environment.ipv6 else []),
        *(["--privileged"] if environment.privileged else []),
        environment.image_ref(registry),
        "bash", "-c", command,
    ], capture_output=True, text=True, timeout=_RUNNER_TIMEOUT)
    return RunResult(environment, result.returncode, (result.stdout + result.stderr)[-_LOG_TAIL:])


def run_environments(target: IntegrationTarget, awslc: Path, registry: str) -> list[RunResult]:
    return [_run_environment(target, environment, awslc, registry) for environment in target.environments]


def run_integration(target: IntegrationTarget, awslc: Path, registry: str) -> AgentTool:
    # Capture trusted paths and image selection here so the agent cannot replace them through tool arguments.
    @tool
    def run_integration() -> str:
        """Run every failed CI environment with the current patches and return each result and log tail."""
        return describe_results(run_environments(target, awslc, registry))

    return run_integration
