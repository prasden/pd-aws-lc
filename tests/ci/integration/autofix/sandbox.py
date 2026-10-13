import os
import platform
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path

import docker
from docker.models.containers import Container
from pydantic import BaseModel
from strands.sandbox.docker import DockerSandbox

from .git import Git
from .target import IntegrationTarget

_RUNNER_TIMEOUT = 3600
_CONTAINER_AWSLC = Path("/codebuild/output/aws-lc")
_CONTAINER_SRC = Path("/autofix-src")
_PROXY_IMAGE = ("public.ecr.aws/docker/library/python:3.13-alpine"
                "@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a")
_PROXY_SCRIPT = Path(__file__).parent / "egress_proxy.py"
_GITHUB = {"github.com", "codeload.github.com", ".githubusercontent.com"}
_PYPI = {"pypi.org", "files.pythonhosted.org"}
_APT = {"archive.ubuntu.com", "security.ubuntu.com", "ports.ubuntu.com"}
_CRATES = {"crates.io", "index.crates.io", "static.crates.io", "static.rust-lang.org"}
_GRADLE = {"services.gradle.org", "plugins.gradle.org", "plugins-artifacts.gradle.org", "downloads.gradle.org",
           "repo.maven.apache.org", "repo1.maven.org"}
_EGRESS = {
    "accp": _GITHUB | _GRADLE,
    "bind9": _APT | _PYPI,
    "crt": _GITHUB,
    "grpc": _GITHUB,
    "httpd": _GITHUB | _APT | _PYPI,
    "librdkafka": _PYPI,
    "nmap": _APT,
    "ntp": {"downloads.nwtime.org"},
    "pyopenssl": _APT | _PYPI | _CRATES,
    "python": _GITHUB | _PYPI,
    "rust_openssl": _CRATES,
    "xtrabackup": _GITHUB,
}


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

    @contextmanager
    def egress(self) -> Iterator[tuple[str, dict[str, str], Container]]:
        """Create an internal network whose only way out is a proxy that allows the target's hosts, and remove both on exit.

        Yields the network name, the proxy env vars for a job, and the proxy container, whose logs list denied hosts.
        Example: pyopenssl may reach pypi.org over HTTPS and archive.ubuntu.com over HTTP, and nothing else.
        """
        network = self.docker.networks.create(f"autofix-{uuid.uuid4().hex[:12]}", internal=True)
        proxy = self.docker.containers.run(
            _PROXY_IMAGE,
            ["python3", "/egress_proxy.py", *sorted(_EGRESS.get(self.target.name, set()))],
            volumes=[f"{_PROXY_SCRIPT}:/egress_proxy.py:ro"],
            user="65534:65534",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            read_only=True,
            detach=True,
        )
        try:
            network.connect(proxy)
            proxy.reload()
            ip = proxy.attrs["NetworkSettings"]["Networks"][network.name]["IPAddress"]
            url = f"http://{ip}:3128"
            yield network.name, {
                "http_proxy": url, "https_proxy": url, "HTTP_PROXY": url, "HTTPS_PROXY": url,
                "JAVA_TOOL_OPTIONS": f"-Dhttp.proxyHost={ip} -Dhttp.proxyPort=3128 "
                                     f"-Dhttps.proxyHost={ip} -Dhttps.proxyPort=3128",
            }, proxy
        finally:
            proxy.remove(force=True)
            network.remove()

    @cache
    def replay(self, diff: str) -> VerifyResult:
        """Replay a diff on a fresh copy of aws-lc in every failed CI job of this arch, the way CI ran them.

        The patches and .git are read-only, each job's git clones copy the local downstream repos, and the job reaches
        only the target's allowed hosts. Each diff runs once, so the check after the agent stops reuses the agent's
        last replay of the same changes.
        """
        git_env = Git.config_env({"safe.directory": "*"} | {
            f"url.file://{_CONTAINER_SRC / repo.name}.insteadOf": repo.url for repo in self.target.repos})
        copy = self.awslc.copy(self.root)
        copy.apply(diff)
        passed, reports = True, []
        for env in self.jobs:
            with self.egress() as (network, proxy_env, proxy):
                container = self.docker.containers.run(
                    f"{self.registry}/aws-lc/{env.image}",
                    ["bash", "-c", self.target.script(env)],
                    working_dir=str(_CONTAINER_AWSLC),
                    volumes=[
                        f"{copy.path}:{_CONTAINER_AWSLC}:rw",
                        *(f"{copy.path / path}:{_CONTAINER_AWSLC / path}:ro" for path in [".git", *self.editable]),
                        f"{self.src}:{_CONTAINER_SRC}:ro",
                    ],
                    network=network,
                    environment=env.env_vars | git_env | proxy_env,
                    sysctls={"net.ipv6.conf.all.disable_ipv6": "0"} if env.ipv6 else {},
                    detach=True,
                )
                try:
                    exit_code = container.wait(timeout=_RUNNER_TIMEOUT)["StatusCode"]
                    passed &= exit_code == 0
                    reports.append(f"{env.arch}/{env.image}: exit {exit_code}\n"
                                   f"{container.logs().decode(errors='replace')}\n"
                                   f"egress proxy:\n{proxy.logs().decode(errors='replace')}")
                finally:
                    container.remove(force=True)
        return VerifyResult(passed=passed, report="\n\n".join(reports))
