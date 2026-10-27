import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Self

from pydantic.dataclasses import dataclass

_COMMIT_SHA = re.compile(r"[0-9a-f]{40}") # Allow only full SHAs and plain folder names from integration-failure.txt,
                                          # because downstream tests can rewrite it.
_REPO_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
_LOCKED_DOWN_GIT = ("-c", "protocol.ext.allow=never", "-c", "protocol.fd.allow=never",
                    "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", "-c", "core.symlinks=false",
                    "-c", "safe.directory=*")


@dataclass(frozen=True)
class Repo:
    url: str
    sha: str

    def __post_init__(self):
        if not (self.url.startswith("https://") and _COMMIT_SHA.fullmatch(self.sha) and _REPO_NAME.fullmatch(self.name)):
            raise ValueError(f"rejected repo from integration-failure.txt: {self.url} {self.sha}")

    @property
    def name(self) -> str:
        return Path(self.url).name.removesuffix(".git")


# Run git in one folder with no credentials, config files, hooks, prompts, symlinks, or ext/fd protocols.
class Git:
    def __init__(self, path: Path):
        self.path = path
        self.env = {"PATH": os.environ["PATH"], "HOME": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

    def run(self, *args: str, input: str = "") -> str:
        return subprocess.run(["git", *_LOCKED_DOWN_GIT, "-C", str(self.path), *args], env=self.env, input=input,
                              capture_output=True, text=True, check=True).stdout

    # Turn git config into env vars, which reach every git call in a process tree, including under su -p.
    @staticmethod
    def config_env(config: dict[str, str]) -> dict[str, str]:
        env = {"GIT_CONFIG_COUNT": str(len(config))}
        for index, (key, value) in enumerate(config.items()):
            env |= {f"GIT_CONFIG_KEY_{index}": key, f"GIT_CONFIG_VALUE_{index}": value}
        return env

    @staticmethod
    def changed_paths(diff: str) -> list[Path]:
        numstat = Git(Path("/")).run("apply", "--numstat", "-", input=diff)
        return [Path(line.split("\t")[-1]) for line in numstat.splitlines()]

    # Clone the repo at its SHA, then point every remote branch and tag at that SHA so a runner cloning a named ref gets it.
    @classmethod
    def clone(cls, repo: Repo, path: Path) -> Self:
        path.mkdir(parents=True)
        git = cls(path)
        git.run("init", "-q")
        git.run("fetch", "-q", "--no-tags", "--depth", "500", repo.url, repo.sha)
        git.run("checkout", "-q", "FETCH_HEAD")
        refs = git.run("ls-remote", "--heads", "--tags", "--refs", repo.url).splitlines()
        git.run("update-ref", "--stdin", input="".join(f"update {ref.partition('\t')[2]} {repo.sha}\n" for ref in refs))
        return git

    # Copy HEAD into a new folder under parent, so a CI replay cannot dirty this checkout.
    def copy(self, parent: Path) -> Self:
        copy = type(self)(Path(tempfile.mkdtemp(prefix="replay-", dir=parent)))
        self.run("clone", "-q", "--no-checkout", str(self.path), str(copy.path))
        copy.run("checkout", "-q", self.run("rev-parse", "HEAD").strip())
        return copy

    def diff(self, paths: list[Path]) -> str:
        self.run("add", "--intent-to-add", "--", *map(str, paths))
        return self.run("diff", "--no-renames", "--", *map(str, paths))

    def apply(self, diff: str) -> None:
        if diff:
            self.run("apply", "-", input=diff)

    def commit(self, branch: str, message: str) -> None:
        self.run("switch", "-q", "-c", branch)
        self.run("add", "-A")
        self.run("-c", "user.name=aws-lc-autofix", "-c", "user.email=aws-lc-autofix@users.noreply.github.com",
                 "commit", "-q", "-m", message)
