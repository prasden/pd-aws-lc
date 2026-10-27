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

from .git import Git
from .target import IntegrationTarget

_RUNNER_TIMEOUT = 3600
_COMMAND_TIMEOUT = 120
_READ_ONLY_PROGRAMS = {"grep", "ls", "cat", "head", "tail", "wc", "diff"}
_GIT_READ_COMMANDS = {"log", "show", "diff", "blame", "grep", "ls-files"}
_GIT_BLOCKED_FLAGS = ("-c", "-O", "--open-files-in-pager", "--output", "--ext-diff")
_SED_PRINT_RANGE = re.compile(r"\d+,\d+[pl]")
_CONTAINER_AWSLC = Path("/aws-lc")
_CONTAINER_SRC = Path("/autofix-src")
_NEEDS_NETWORK = {"accp", "bind9", "crt", "grpc", "httpd", "librdkafka", "nmap", "ntp", "pyopenssl", "python",
                  "rust_openssl", "xtrabackup"}


class ReplayResult(BaseModel):
    passed: bool
    report: str


class Sandbox:
    def __init__(self, root: Path, target: IntegrationTarget, registry: str):
        """Clone aws-lc and the downstream repos under root at their failing commits, and fix what the agent may edit.

        Only the failed jobs of this machine's arch are replayed, and a machine with none of them raises.
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

        patch_dirs = target.patch_dirs(self.awslc.path)
        for patch_dir in patch_dirs:
            (self.awslc.path / patch_dir).mkdir(exist_ok=True)
        self.editable = [*patch_dirs, target.runner]

    @contextmanager
    def agent_container(self) -> Iterator[DockerSandbox]:
        """Run the agent's container and remove it on exit.

        The sandbox is read-only except the patch dirs and runner script, with no network, no capabilities, and the host user.
        The agent's file tools and run tool run inside it through docker exec.
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
        """Replay every failed CI job on a fresh copy of aws-lc with your current changes and return each exit code."""
        return self.replay(self.awslc.diff(self.editable))

    @tool(context=True)
    async def run(self, argv: list[str], tool_context: ToolContext) -> dict[str, str | int]:
        """Run one allowlisted read-only command in the sandbox and return its output and exit code.

        Pass the command as a list of arguments, not a shell string. Pipes, redirects, globs, and variables do not work.
        Allowed: git log, show, diff, blame, grep, or ls-files; patch --dry-run; sed -n 'START,ENDp' or 'START,ENDl';
        grep, ls, cat, head, tail, wc, and diff.
        """
        self._check(argv)
        result = await tool_context.agent.sandbox.execute(shlex.join(argv), timeout=_COMMAND_TIMEOUT)
        return {"output": result.stdout, "error": result.stderr, "exit_code": result.exit_code}

    # Raise ValueError naming the rule a command breaks, so only read-only allowlisted commands reach the sandbox.
    # Example: ["sed", "-n", "10,20l", "x.patch"] passes, ["sed", "-i", "s/a/b/", "x.patch"] raises.
    @staticmethod
    def _check(argv: list[str]) -> None:
        match argv:
            case [program, *_] if program in _READ_ONLY_PROGRAMS:
                return
            case ["git", "-C", _, command, *args] | ["git", command, *args] if (
                    command in _GIT_READ_COMMANDS and not any(arg.startswith(_GIT_BLOCKED_FLAGS) for arg in args)):
                return
            case ["patch", *args] if "--dry-run" in args and "-e" not in args and "--ed" not in args:
                return
            case ["sed", "-n", script, _] if _SED_PRINT_RANGE.fullmatch(script):
                return
        raise ValueError(f"not allowed: {shlex.join(argv)}")

    @cache
    def replay(self, diff: str) -> ReplayResult:
        """Replay a diff on a fresh copy of aws-lc in every failed CI job of this arch, the way CI ran them.

        The patches and .git are read-only, and each job's git clones copy the local downstream repos. Each diff runs
        once, so the check after the agent stops reuses the agent's last replay of the same changes.
        """
        git_env = Git.config_env({"safe.directory": "*"} | {
            f"url.file://{_CONTAINER_SRC / repo.name}.insteadOf": repo.url for repo in self.target.repos})
        copy = self.awslc.copy(self.root)
        copy.apply(diff)
        passed, reports = True, []
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
                network_mode="bridge" if self.target.name in _NEEDS_NETWORK else "none",
                environment=env.env_vars | git_env,
                sysctls={"net.ipv6.conf.all.disable_ipv6": "0"} if env.ipv6 else {},
                detach=True,
            )
            try:
                exit_code = container.wait(timeout=_RUNNER_TIMEOUT)["StatusCode"]
                passed &= exit_code == 0
                reports.append(f"{env.arch}/{env.image}: exit {exit_code}\n{container.logs().decode(errors='replace')}")
            finally:
                container.remove(force=True)
        return ReplayResult(passed=passed, report="\n\n".join(reports))
