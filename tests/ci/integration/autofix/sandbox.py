import os
import platform
import re
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path

import docker
from pydantic import BaseModel
from strands import ToolContext, tool
from strands.sandbox.docker import DockerSandbox
from strands.vended_tools.shell.types import ShellOutput

from .git import Git
from .target import IntegrationTarget

_RUNNER_TIMEOUT = 3600
_COMMAND_TIMEOUT = 120
_READ_ONLY_PROGRAMS = {"grep", "ls", "cat", "head", "tail", "wc", "diff"}
_GIT_READ_COMMANDS = {"log", "show", "diff", "blame", "grep", "ls-files"}
_GIT_BLOCKED_OPTIONS = ("--open-files-in-pager", "--output", "--ext-diff")
_SED_PRINT_RANGE = re.compile(r"\d+,\d+[pl]")
_CONTAINER_AWSLC = Path("/aws-lc")
_CONTAINER_SRC = Path("/autofix-src")
_RUN_DESCRIPTION = (
    "Run one read-only command in the sandbox and return its output, error, and exit code.\n\n"
    "argv is a list of arguments, not a shell string, so pipes, redirects, globs, and variables do not work.\n"
    f"Allowed: {', '.join(sorted(_READ_ONLY_PROGRAMS))}; sed -n 'A,Bp' or 'A,Bl'; patch --dry-run; "
    f"git -C aws-lc|src/<repo> {'|'.join(sorted(_GIT_READ_COMMANDS))}."
)


def _runs_or_writes(arg: str) -> bool:
    """Return whether a git option can start a program or write a file.

    The run tool uses this to keep the agent's git commands read-only. For example, "git grep -O sh" would run sh.
    Abbreviations and bundled short flags count, so "--outp=x" and "-iOsh" match but "--oneline" does not.
    """
    name = arg.split("=", 1)[0]
    if name.startswith("--"):
        return len(name) > 2 and any(option.startswith(name) for option in _GIT_BLOCKED_OPTIONS)
    return name.startswith("-") and "O" in name


class ReplayResult(BaseModel):
    passed: bool
    report: str


class Sandbox:
    def __init__(self, root: Path, target: IntegrationTarget, registry: str):
        """Clone aws-lc and the downstream repos at their failing commits, and set what the agent may edit.

        Raise ValueError if this machine's arch has no failed job, since a replay of no jobs would pass.
        """
        self.jobs = target.on(platform.machine())
        if not self.jobs:
            raise ValueError(f"{target.dir_name} has no failed CI job on {platform.machine()}")
        self.root = root.resolve()
        self.target = target
        self.images = f"{registry}/aws-lc"
        self.docker = docker.from_env()
        self.awslc = Git.clone(target.awslc, self.root / "aws-lc")
        self.src = self.root / "src"
        for repo in target.repos:
            Git.clone(repo, self.src / repo.name)
        self.git_repos = {"aws-lc", *(f"src/{repo.name}" for repo in target.repos)}

        patch_dirs = target.patch_dirs(self.awslc.path)
        for patch_dir in patch_dirs:
            (self.awslc.path / patch_dir).mkdir(exist_ok=True)
        self.editable = [*patch_dirs, target.runner]

    @contextmanager
    def agent_container(self) -> Iterator[DockerSandbox]:
        """Start an idle container for the agent's tools to run in, and remove it on exit.

        Everything is read-only except the editable paths. The container has no network and no Linux capabilities.
        """
        container = self.docker.containers.run(
            f"{self.images}/{self.jobs[0].image}",
            ["sleep", "infinity"],
            working_dir=str(self.root),
            volumes=[
                f"{self.root}:{self.root}:ro",
                *(f"{self.awslc.path / path}:{self.awslc.path / path}:rw" for path in self.editable),
            ],
            network_mode="none",
            user=f"{os.getuid()}:{os.getgid()}",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            detach=True,
        )
        try:
            yield DockerSandbox(container.name)
        finally:
            container.remove(force=True)

    @tool
    def run_integration(self) -> ReplayResult:
        """Replay every failed CI job on a fresh copy of aws-lc with your current changes.

        Return whether all jobs passed, plus each job's exit code and logs.
        """
        return self.replay(self.awslc.diff(self.editable))

    @tool(context=True, description=_RUN_DESCRIPTION)
    async def run(self, argv: list[str], tool_context: ToolContext) -> ShellOutput:
        self._check(argv)
        result = await tool_context.agent.sandbox.execute(shlex.join(argv), timeout=_COMMAND_TIMEOUT)
        return {"output": result.stdout, "error": result.stderr, "exit_code": result.exit_code}

    def _check(self, argv: list[str]) -> None:
        match argv:
            case [program, *_] if program in _READ_ONLY_PROGRAMS:
                return
            case ["git", "-C", repo, command, *args] if (
                    repo in self.git_repos and command in _GIT_READ_COMMANDS and not any(map(_runs_or_writes, args))):
                return
            case ["patch", *args] if "--dry-run" in args:
                return
            case ["sed", "-n", script, _] if _SED_PRINT_RANGE.fullmatch(script):
                return
        raise ValueError(f"not allowed: {shlex.join(argv)}")

    @cache
    def replay(self, diff: str) -> ReplayResult:
        """Rerun this arch's failed CI jobs on a fresh aws-lc copy with the diff applied and local downstream repos."""
        git_env = Git.config_env({"safe.directory": "*"} | {
            f"url.file://{_CONTAINER_SRC / repo.name}.insteadOf": repo.url for repo in self.target.repos
        })
        copy = self.awslc.copy(self.root)
        copy.apply(diff)
        passed, reports = True, []

        try:
            for env in self.jobs:
                container = self.docker.containers.run(
                    f"{self.images}/{env.image}",
                    ["bash", "-c", self.target.script(env)],
                    working_dir=str(_CONTAINER_AWSLC),
                    volumes=[
                        f"{copy.path}:{_CONTAINER_AWSLC}:rw",
                        *(f"{copy.path / path}:{_CONTAINER_AWSLC / path}:ro" for path in [".git", *self.editable]),
                        f"{self.src}:{_CONTAINER_SRC}:ro",
                    ],
                    network_mode="bridge",
                    environment=env.env_vars | git_env,
                    sysctls={"net.ipv6.conf.all.disable_ipv6": "0"} if env.ipv6 else {},
                    security_opt=["no-new-privileges"],
                    init=True,
                    detach=True,
                )
                try:
                    exit_code = container.wait(timeout=_RUNNER_TIMEOUT)["StatusCode"]
                    passed &= exit_code == 0
                    reports.append(f"{env.arch}/{env.image}: exit {exit_code}\n{container.logs().decode(errors='replace')}")
                finally:
                    container.remove(force=True)
        finally:
            # Delete the copy from a container. The replay leaves files that only the container's root can delete.
            self.docker.containers.run(
                f"{self.images}/{self.jobs[0].image}",
                ["find", str(_CONTAINER_AWSLC), "-mindepth", "1", "-delete"],
                volumes=[f"{copy.path}:{_CONTAINER_AWSLC}:rw"],
                network_mode="none",
                remove=True,
            )
            copy.path.rmdir()
        return ReplayResult(passed=passed, report="\n\n".join(reports))
