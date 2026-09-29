import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

# Allow only plain names, versions, and SHAs from integration-failure.txt, because downstream tests can rewrite it.
# Example: a commit sha of "--upload-pack=cmd" would run cmd in git fetch, so anything but 40 hex chars is rejected.
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_REPO_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
_INTEGRATION_NAME = re.compile(r"[a-z0-9_]+")
_VERSION = re.compile(r"[A-Za-z0-9._/-]*")


@dataclass(frozen=True)
class Environment:
    job_id: str
    image: str
    compiler: str
    arch: Literal["x86_64", "aarch64"]
    user: str | None
    ipv6: bool
    privileged: bool
    env_vars: dict[str, str]

    @classmethod
    def from_job(cls, job: dict) -> Self:
        return cls(
            job_id=job["job_id"],
            image=job["image"],
            compiler=job["compiler"],
            arch=job["arch"],
            user=job["user"] if "user" in job else None,
            ipv6="ipv6" in job,
            privileged="options" in job,
            env_vars=job["env"],
        )

    def image_ref(self, registry: str) -> str:
        return f"{registry}/aws-lc/{self.image}"


@dataclass(frozen=True)
class Repo:
    url: str
    sha: str

    # Reject a repo before git sees it unless it is an https URL at a full SHA with a safe folder name.
    # Example: file:///etc, ext::sh -c ..., and https://github.com/x/.. all raise ValueError.
    def __post_init__(self):
        if not self.url.startswith("https://") or not _COMMIT_SHA.fullmatch(self.sha) or not _REPO_NAME.fullmatch(self.name):
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
        if not _INTEGRATION_NAME.fullmatch(self.name) or not _VERSION.fullmatch(self.version) or ".." in self.version:
            raise ValueError(f"rejected integration from integration-failure.txt: {self.name!r} {self.version!r}")

    @property
    def dir_name(self) -> str:
        return f"{self.name}-{self.version}".replace("/", "-") if self.version else self.name

    # Example: tpm2_tss gives tpm2_tools_patch and tpm2_tss_patch, and an unpatched integration gets a new <name>_patch.
    def runner(self, awslc: Path) -> Path:
        return awslc / "tests/ci/integration" / f"run_{self.name}_integration.sh"


    def patch_dirs(self, awslc: Path) -> list[Path]:
        integration_dir = awslc / "tests/ci/integration"
        names = sorted(set(re.findall(r"[a-z0-9_]+_patch", self.runner(awslc).read_text())))
        existing = [integration_dir / name for name in names if (integration_dir / name).is_dir()]
        return existing or [integration_dir / f"{self.name}_patch"]

    @classmethod
    def parse(cls, integration_failure_txt: str, awslc_url: str) -> Self:

        lines = integration_failure_txt.splitlines()
        integration_name, integration_version = lines[0].split("\t")
        commits = [line.removeprefix("commit=").split() for line in lines if line.startswith("commit=")]

        return cls(
            name=integration_name,
            version=integration_version,
            awslc_repo=Repo(url=awslc_url, sha=lines[1].removeprefix("awslc_sha=")),
            integration_repos=tuple(Repo(url=url, sha=sha) for url, sha in commits),
            environments=(Environment.from_job(json.loads(lines[-1].removeprefix("job="))),),
        )

    @classmethod
    def from_artifacts(cls, artifacts: Path, repo: str) -> list[Self]:
        awslc_url = f"https://github.com/{repo}.git"
        # {(integration name, integration version): target}
        targets: dict[tuple[str, str], Self] = {}

        for path in sorted(artifacts.glob("*/integration-failure.txt")):
            failure = cls.parse(path.read_text(), awslc_url)
            key = (failure.name, failure.version)

            existing = targets.get(key)
            if existing is not None:
                existing.environments += failure.environments
            else:
                targets[key] = failure

        return [targets[key] for key in sorted(targets)]
