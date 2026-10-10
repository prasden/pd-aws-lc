import os
import re
import shlex
from dataclasses import field
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import StringConstraints, TypeAdapter
from pydantic.dataclasses import dataclass
from strands.sandbox.errors import SandboxTimeoutError
from strands.sandbox.types import ExecutionResult

from .container import Access, Container, Mount, Network, Variables

# Allow only plain names, versions, and SHAs from integration-failure.txt, because downstream tests can rewrite it.
# Example: a commit sha of "--upload-pack=cmd" would run cmd in git fetch, so anything but 40 hex chars is rejected.
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_REPO_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
_INTEGRATION_NAME = re.compile(r"[a-z0-9_]+")
_VERSION = re.compile(r"[A-Za-z0-9._/-]*")
type Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]*$")]

_RUNNER_TIMEOUT = 3600
_RUNNER_LOG_TAIL = 8000
_CONTAINER_AWSLC = Path("/aws-lc")
_CONTAINER_SRC = Path("/autofix-src")
# List the integrations whose runners download packages, submodules, or tarballs that the precloned repos lack.
# Example: python installs awscrt with pip, so it keeps the network, and openssh only clones, so it runs offline.
_NEEDS_NETWORK = {"accp", "bind9", "crt", "grpc", "httpd", "librdkafka", "nmap", "ntp", "pyopenssl", "python",
                  "rust_openssl", "xtrabackup"}


# Pass git config as environment variables, which reach every git call in the container, including under su -p.
# Example: {"safe.directory": "*"} gives GIT_CONFIG_COUNT=1, GIT_CONFIG_KEY_0=safe.directory, GIT_CONFIG_VALUE_0=*.
def git_config_env(config: Variables) -> Variables:
    variables = {"GIT_CONFIG_COUNT": str(len(config))}
    for index, (key, value) in enumerate(config.items()):
        variables |= {f"GIT_CONFIG_KEY_{index}": key, f"GIT_CONFIG_VALUE_{index}": value}
    return variables


@dataclass(frozen=True)
class Sandbox:
    root: Path

    @property
    def awslc(self) -> Path:
        return self.root / "aws-lc"

    @property
    def src(self) -> Path:
        return self.root / "src"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def out(self) -> Path:
        return self.root / "out"

    @property
    def verify(self) -> Path:
        return self.root / "verify"


@dataclass(frozen=True)
class Environment:
    image: Token
    compiler: Token
    arch: Literal["x86_64", "aarch64"]
    job_id: int = 0
    user: Token = ""
    ipv6: bool = False
    env_vars: Variables = field(default_factory=dict)

    def image_ref(self, registry: str) -> str:
        return f"{registry}/aws-lc/{self.image}"

    # Build a bash command that stubs the aws CLI, sources the compiler setup when present, and runs as the CI user.
    # Example: postgres jobs chown aws-lc to postgres and run the runner through su -p so the environment survives.
    def command(self, runner: Path, version: str) -> str:
        setup = f"/opt/compiler-env/setup-{self.compiler}.sh"
        command = "; ".join([
            "aws() { :; }",
            "export -f aws",
            f"if [ -f {setup} ]; then source {setup}; fi",
            f"{runner} {shlex.quote(version)}",
        ])
        if self.user:
            command = "; ".join([
                f"mkdir -p /home/{self.user}",
                f"chown -R {self.user} /home/{self.user} {_CONTAINER_AWSLC} 2>/dev/null",
                f"su -p {self.user} -c {shlex.quote(command)}",
            ])
        return f"bash -c {shlex.quote(command)}"


@dataclass(frozen=True)
class Repo:
    url: str
    sha: str

    # Reject a repo before git sees it unless it is an https URL at a full SHA with a safe folder name.
    # Example: file:///etc, ext::sh -c ..., and https://github.com/x/.. all raise ValueError.
    def __post_init__(self):
        if not (self.url.startswith("https://") and _COMMIT_SHA.fullmatch(self.sha) and _REPO_NAME.fullmatch(self.name)):
            raise ValueError(f"rejected repo from integration-failure.txt: {self.url} {self.sha}")

    @property
    def name(self) -> str:
        return Path(self.url).name.removesuffix(".git")


@dataclass
class IntegrationTarget:
    name: str
    version: str
    awslc_repo: Repo
    integration_repos: tuple[Repo, ...]
    environments: tuple[Environment, ...]

    # Reject a name or version that could escape the work dir or run as shell code in the runner command.
    # Example: "ruby<TAB>../../x" and "ruby<TAB>$(id)" raise ValueError, and "openvpn<TAB>release/2.6" passes.
    def __post_init__(self):
        safe_version = _VERSION.fullmatch(self.version) and ".." not in self.version
        if not (_INTEGRATION_NAME.fullmatch(self.name) and safe_version):
            raise ValueError(f"rejected integration from integration-failure.txt: {self.name!r} {self.version!r}")

    # Group integration-failure.txt reports by integration and version, one environment per failed CI job.
    # Example: ruby master FIPS and non-FIPS reports give one ruby-master target with two environments.
    @classmethod
    def from_reports(cls, reports: list[str], awslc_repo: Repo) -> list[Self]:
        targets: dict[tuple[str, str], Self] = {}
        for report in sorted(reports):
            lines = report.splitlines()
            name, version = lines[0].split("\t")
            repos = tuple(Repo(*line.removeprefix("commit=").split()) for line in lines if line.startswith("commit="))
            target = targets.setdefault((name, version), cls(name, version, awslc_repo, repos, ()))
            target.environments += (TypeAdapter(Environment).validate_json(lines[-1].removeprefix("job=")),)
        return list(targets.values())

    @classmethod
    def load(cls, path: Path) -> Self:
        return TypeAdapter(cls).validate_json(path.read_text())

    def save(self, path: Path) -> None:
        path.write_bytes(TypeAdapter(type(self)).dump_json(self, indent=2))

    @property
    def dir_name(self) -> str:
        return f"{self.name}-{self.version}".replace("/", "-") if self.version else self.name

    def runner(self, awslc: Path) -> Path:
        return awslc / "tests/ci/integration" / f"run_{self.name}_integration.sh"

    # List the patch dirs the runner names, or a new <name>_patch when none exist.
    # Example: tpm2_tss gives tpm2_tools_patch and tpm2_tss_patch, and an unpatched integration gets a new <name>_patch.
    def patch_dirs(self, awslc: Path) -> list[Path]:
        integration_dir = awslc / "tests/ci/integration"
        names = sorted(set(re.findall(r"[a-z0-9_]+_patch", self.runner(awslc).read_text())))
        existing = [integration_dir / name for name in names if (integration_dir / name).is_dir()]
        return existing or [integration_dir / f"{self.name}_patch"]

    # List the paths the agent may change, refusing any that a CI run swapped for a symlink to elsewhere on the host.
    # Example: a kafka_patch -> /etc link planted by downstream test code raises instead of mounting /etc.
    def writable(self, awslc: Path) -> list[Path]:
        paths = [*self.patch_dirs(awslc), self.runner(awslc)]
        if any(path.resolve() != path for path in paths):
            raise ValueError(f"refusing a symlinked patch dir or runner under {awslc}")
        return paths

    # Give the agent the sandbox read-only except the patch dirs and runner, offline, unprivileged, and as the host user.
    # Example: the agent can edit aws-lc/tests/ci/integration/ruby_patch but not aws-lc/.git or logs/.
    def agent_container(self, sandbox: Sandbox, registry: str) -> Container:
        editable = [Mount(path, path) for path in self.writable(sandbox.awslc)]
        return Container(
            name=f"autofix-agent-{self.dir_name}",
            image=self.environments[0].image_ref(registry),
            workdir=sandbox.root,
            mounts=[Mount(sandbox.root, sandbox.root, Access.READ_ONLY), *editable],
            user=f"{os.getuid()}:{os.getgid()}",
            locked_down=True,
        )

    # Replay one CI job against the patched aws-lc, cloning downstream repos from the local copies through git redirects.
    # Example: openssh's git clone https://github.com/openssh/openssh-portable.git copies /autofix-src/openssh-portable.
    def ci_container(self, env: Environment, sandbox: Sandbox, registry: str) -> Container:
        clones = {f"url.file://{_CONTAINER_SRC / repo.name}.insteadOf": repo.url for repo in self.integration_repos}
        frozen = [Mount(path, _CONTAINER_AWSLC / path.relative_to(sandbox.awslc), Access.READ_ONLY)
                  for path in [sandbox.awslc / ".git", *self.writable(sandbox.awslc)]]
        return Container(
            name=f"autofix-ci-{self.dir_name}",
            image=env.image_ref(registry),
            workdir=_CONTAINER_AWSLC,
            mounts=[Mount(sandbox.awslc, _CONTAINER_AWSLC), *frozen, Mount(sandbox.src, _CONTAINER_SRC, Access.READ_ONLY)],
            network=Network.BRIDGE if self.name in _NEEDS_NETWORK else Network.NONE,
            env=env.env_vars | git_config_env({"safe.directory": "*"} | clones),
            sysctls={"net.ipv6.conf.all.disable_ipv6": "0"} if env.ipv6 else {},
        )

    # Run the runner in every failed CI environment and return whether all passed, with each environment's log tail.
    # Example: ruby on x86_64 and aarch64 returns (False, "x86_64/ubuntu:24.04: PASSED ...\n\naarch64/ubuntu:24.04: FAILED (exit 1) ...").
    async def run(self, sandbox: Sandbox, registry: str) -> tuple[bool, str]:
        passed, report = True, []
        for env in self.environments:
            with self.ci_container(env, sandbox, registry).start() as container:
                try:
                    result = await container.execute(env.command(self.runner(_CONTAINER_AWSLC), self.version),
                                                     timeout=_RUNNER_TIMEOUT)
                except SandboxTimeoutError as timeout:
                    result = ExecutionResult(exit_code=124, stdout=timeout.stdout, stderr=timeout.stderr)
            passed &= result.exit_code == 0
            status = "PASSED" if result.exit_code == 0 else f"FAILED (exit {result.exit_code})"
            report.append(f"{env.arch}/{env.image}: {status}\n{(result.stdout + result.stderr)[-_RUNNER_LOG_TAIL:]}")
        return passed, "\n\n".join(report)
