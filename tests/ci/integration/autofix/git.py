import base64
import os
import re
import subprocess
from pathlib import Path

from pydantic.dataclasses import dataclass

# Allow only full SHAs and plain folder names from integration-failure.txt, because downstream tests can rewrite it.
# Example: a commit sha of "--upload-pack=cmd" would run cmd in git fetch, so anything but 40 hex chars is rejected.
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_REPO_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
_HISTORY_DEPTH = "100"
_LOCKED_DOWN_GIT = ("-c", "protocol.ext.allow=never", "-c", "protocol.fd.allow=never",
                    "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", "-c", "core.symlinks=false",
                    "-c", "safe.directory=*")
_BOT_IDENTITY = ("-c", "user.name=aws-lc-autofix", "-c", "user.email=aws-lc-autofix@users.noreply.github.com")


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


# Pass git config as environment variables, which reach every git call in the container, including under su -p.
# Example: {"safe.directory": "*"} gives GIT_CONFIG_COUNT=1, GIT_CONFIG_KEY_0=safe.directory, GIT_CONFIG_VALUE_0=*.
def git_config_env(config: dict[str, str]) -> dict[str, str]:
    variables = {"GIT_CONFIG_COUNT": str(len(config))}
    for index, (key, value) in enumerate(config.items()):
        variables |= {f"GIT_CONFIG_KEY_{index}": key, f"GIT_CONFIG_VALUE_{index}": value}
    return variables


# Run git with no credentials, config files, hooks, prompts, symlinks, or ext/fd protocols, for untrusted URLs and SHAs.
# Example: _git("ls-remote", url) for a URL taken from an artifact never sees GH_TOKEN or the AWS keys.
def _git(*args: str, input: str = "", **env: str) -> str:
    base_env = {"PATH": os.environ["PATH"], "HOME": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.run(["git", *_LOCKED_DOWN_GIT, *args], env=base_env | env, input=input,
                          capture_output=True, text=True, check=True).stdout


# Clone the repo at its SHA, then point every remote branch and tag at that SHA so a runner cloning a named ref gets it.
# Example: a runner's git clone --branch V_9_9 through the url redirect checks out the exact failing commit.
def clone(repo: Repo, destination: Path) -> None:
    checkout = str(destination)
    _git("init", "-q", checkout)
    _git("-C", checkout, "fetch", "-q", "--no-tags", "--depth", _HISTORY_DEPTH, repo.url, repo.sha)
    _git("-C", checkout, "checkout", "-q", "FETCH_HEAD")
    branches_and_tags = _git("ls-remote", "--heads", "--tags", "--refs", repo.url).splitlines()
    pin_refs = "".join(f"update {line.partition('\t')[2]} {repo.sha}\n" for line in branches_and_tags)
    _git("-C", checkout, "update-ref", "--stdin", input=pin_refs)


# Diff the paths, including files the agent created, which git diff skips while they are untracked.
def diff(checkout: Path, paths: list[Path], *options: str) -> str:
    _git("-C", str(checkout), "add", "--intent-to-add", "--", *map(str, paths))
    return _git("-C", str(checkout), "diff", *options, "--", *map(str, paths))


def changed_files(checkout: Path, paths: list[Path]) -> list[Path]:
    return [checkout / name for name in diff(checkout, paths, "--name-only").splitlines()]


def apply(checkout: Path, patch: Path) -> None:
    _git("-C", str(checkout), "apply", str(patch))


def commit(checkout: Path, branch: str, message: str) -> None:
    _git("-C", str(checkout), "switch", "-q", "-c", branch)
    _git("-C", str(checkout), "add", "-A")
    _git("-C", str(checkout), *_BOT_IDENTITY, "commit", "-q", "-m", message)


# Fetch the full history so a push works even when the fork lacks the commits below the shallow clone.
def unshallow(checkout: Path, repo: Repo) -> None:
    _git("-C", str(checkout), "fetch", "-q", "--unshallow", "--no-tags", repo.url, repo.sha)


# Push HEAD to a branch, passing the token in a header so it never appears in argv.
# Example: push(checkout, "https://github.com/crypto-alg/aws-lc.git", "autofix/2026-10-09-ruby-master", token).
def push(checkout: Path, url: str, branch: str, token: str) -> None:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    auth = git_config_env({"http.https://github.com/.extraheader": f"AUTHORIZATION: basic {basic}"})
    _git("-C", str(checkout), "push", "-q", url, f"HEAD:refs/heads/{branch}", **auth)
