import os
import platform
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path

import docker
from pydantic import BaseModel
from strands.sandbox.docker import DockerSandbox

from .git import Git
from .target import IntegrationTarget

_RUNNER_TIMEOUT = 2 * 3600
_CONTAINER_AWSLC = Path("/codebuild/output/aws-lc")
_CONTAINER_SRC = Path("/autofix-src")
# Integrations whose runner downloads more than the recorded repos, such as apt, pip, cargo, gradle, or submodules.
# Example: pyopenssl runs apt-get, pip install, and cargo install, so its replay gets the bridge network.
_NEEDS_NETWORK = {"accp", "bind9", "crt", "grpc", "httpd", "librdkafka", "nmap", "ntp", "pyopenssl", "python",
                  "rust_openssl", "xtrabackup"}


class VerifyResult(BaseModel):
    passed: bool
    report: str


class Sandbox:
    def __init__(self, root: Path, target: IntegrationTarget, registry: str):
        """Clone aws-lc and the downstream repos at their failing commits, and fix what the agent may edit.

        Only the failed jobs of this machine's arch are replayed, and a machine with none of them raises.

        Args:
            root: Work folder for the clones: aws-lc/ and src/<repo>/.
            target: The failed integration and its CI jobs.
            registry: ECR registry that holds the aws-lc CI images.
        """
        self.jobs = target.on(platform.machine())
        if not self.jobs:
            raise ValueError(f"{target.dir_name} has no failed CI job on {platform.machine()}")
        self.root = root.resolve()
        self.target = target
        self.registry = registry
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
        The agent's shell and file tools run inside it through docker exec.
        """
        container = self.docker.containers.run(
            f"{self.registry}/aws-lc/{self.jobs[0].image}",
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

    @cache
    def replay(self, diff: str) -> VerifyResult:
        """Replay a diff on a fresh copy of aws-lc in every failed CI job of this arch, the way CI ran them.

        The patches and .git are read-only, each job's git clones copy the local downstream repos, and only the
        integrations that download dependencies get a network. Each diff runs once, so the check after the agent
        stops reuses the agent's last replay of the same changes.
        """
        git_env = Git.config_env({"safe.directory": "*"} | {
            f"url.file://{_CONTAINER_SRC / repo.name}.insteadOf": repo.url for repo in self.target.repos})
        copy = self.awslc.copy(self.root)
        copy.apply(diff)
        passed, reports = True, []
        for env in self.jobs:
            container = self.docker.containers.run(
                f"{self.registry}/aws-lc/{env.image}",
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
        return VerifyResult(passed=passed, report="\n\n".join(reports))
