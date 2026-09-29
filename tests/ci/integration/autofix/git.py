import os
import re
import subprocess
import tempfile
from pathlib import Path

from .integration import IntegrationTarget, Repo

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HISTORY_DEPTH = "100"
_LOCKED_DOWN_GIT = ("-c", "protocol.ext.allow=never", "-c", "protocol.fd.allow=never",
                    "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null")


def _clean_log(log: str) -> str:
    return "".join(char for char in _ANSI_ESCAPE.sub("", log) if char.isprintable() or char in "\t\n")


# ---------- Credentialed GitHub API ----------
# These methods call gh to download workflow artifacts and job logs, so they intentionally inherit GH_TOKEN
# from the workflow step. They only communicate with the GitHub API and do not operate on artifact-provided
# repository URLs.
class GitHubClient:
    def __init__(self, repo: str):
        self.repo = repo

    def _gh(self, *args: str) -> str:
        return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout

    def failure_targets(self, run_id: str) -> list[IntegrationTarget]:
        with tempfile.TemporaryDirectory() as artifacts:
            self._gh("run", "download", run_id, "--repo", self.repo,
                     "--pattern", "integration-failure-*", "--dir", artifacts)
            return IntegrationTarget.from_artifacts(Path(artifacts), self.repo)

    def job_logs(self, target: IntegrationTarget) -> dict[str, str]:
        return {environment.job_id: _clean_log(self._gh("api", f"/repos/{self.repo}/actions/jobs/{environment.job_id}/logs"))
                for environment in target.environments}


# ---------- Credential-free Git operations ----------
# Repository URLs and commit SHAs come from downloaded failure artifacts and are treated as untrusted input.
# Git subprocesses therefore receive a minimal allowlisted environment without GitHub, AWS, SSH, or other
# workflow credentials. System and user Git configuration, credential helpers, hooks, interactive prompts, and
# the ext and fd protocols are also disabled. This is used to give the fixer agent a sandbox with both repos already
# cloned for it to view.
class GitClient:
    def clone(self, repo: Repo, destination: Path) -> None:
        checkout = str(destination)
        self.git("init", "-q", checkout)
        self.git("-C", checkout, "fetch", "-q", "--no-tags", "--depth",
                 _HISTORY_DEPTH, repo.url, repo.sha)
        self.git("-C", checkout, "checkout", "-q", "FETCH_HEAD")

    # Diff the paths, including files the agent created, which git diff skips while they are untracked.
    # Example: a new ruby_patch/master/b.patch shows up as added lines, and "--name-only" lists it by path.
    def diff(self, checkout: Path, paths: list[Path], *options: str) -> str:
        pathspec = [str(path) for path in paths]
        self.git("-C", str(checkout), "add", "--intent-to-add", "--", *pathspec)
        return self.git("-C", str(checkout), "diff", *options, "--", *pathspec)

    def git(self, *args: str) -> str:
        env = {"PATH": os.environ["PATH"], "HOME": os.devnull, "GIT_TERMINAL_PROMPT": "0",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        return subprocess.run(["git", *_LOCKED_DOWN_GIT, *args], env=env, capture_output=True, text=True,
                              check=True).stdout
