import base64
import io
import os
import re
import subprocess
import zipfile
from pathlib import Path

import requests
from github import Auth, Github
from github.WorkflowJob import WorkflowJob

from .integration import IntegrationTarget, Repo, git_config_env

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HISTORY_DEPTH = "100"
_LOCKED_DOWN_GIT = ("-c", "protocol.ext.allow=never", "-c", "protocol.fd.allow=never",
                    "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", "-c", "core.symlinks=false",
                    "-c", "safe.directory=*")
_BOT_IDENTITY = ("-c", "user.name=aws-lc-autofix", "-c", "user.email=aws-lc-autofix@users.noreply.github.com")


def _clean_log(log: str) -> str:
    return "".join(char for char in _ANSI_ESCAPE.sub("", log) if char.isprintable() or char in "\t\n")


# Talk to GitHub with GH_TOKEN: read run artifacts and job logs, push the reviewed fix, and open its PR.
# Example: GitHubClient("aws/aws-lc").failure_targets(37706164434) never touches a URL taken from an artifact.
class GitHubClient:
    def __init__(self, repo: str):
        self.token = os.environ["GH_TOKEN"]
        self.repo = Github(auth=Auth.Token(self.token)).get_repo(repo)

    def _download(self, url: str) -> bytes:
        response = requests.get(url, headers={"Authorization": f"Bearer {self.token}"}, timeout=120)
        response.raise_for_status()
        return response.content

    # Read the run's failure reports, taking the aws-lc commit from the run itself because the reports are untrusted.
    # Example: a report that names an attacker's aws-lc fork commit still repairs the commit the nightly tested.
    def failure_targets(self, run_id: int) -> list[IntegrationTarget]:
        run = self.repo.get_workflow_run(run_id)
        reports = []
        for artifact in run.get_artifacts():
            if artifact.name.startswith("integration-failure-"):
                with zipfile.ZipFile(io.BytesIO(self._download(artifact.archive_download_url))) as archive:
                    reports.append(archive.read("integration-failure.txt").decode())
        return IntegrationTarget.from_reports(reports, Repo(self.repo.clone_url, run.head_sha))

    def job_logs(self, run_id: int, target: IntegrationTarget) -> dict[int, str]:
        return {job.id: _clean_log(self._download(f"{job.url}/logs").decode(errors="replace"))
                for job in self._failed_jobs(run_id, target)}

    # Pick the target's failed jobs by the ids in its reports, or by job name when the reports predate job ids.
    # Example: ruby master matches ruby-master-x86_64 and ruby-master-fips-x86_64 but not ruby-3.4-x86_64.
    def _failed_jobs(self, run_id: int, target: IntegrationTarget) -> list[WorkflowJob]:
        failed = [job for job in self.repo.get_workflow_run(run_id).jobs() if job.conclusion == "failure"]
        if job_ids := {env.job_id for env in target.environments if env.job_id}:
            return [job for job in failed if job.id in job_ids]
        prefix = target.name.replace("_", "-")
        return ([job for job in failed if job.name.startswith(f"{prefix}-{target.version}")]
                or [job for job in failed if job.name.startswith(prefix)])

    # Push the committed fix to a branch on the fork, passing the token in a header so it never appears in argv.
    # Example: ruby master pushes autofix/2026-10-09-ruby-master to prasden/pd-aws-lc.
    def push(self, checkout: Path, fork: str, branch: str) -> None:
        basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        auth = git_config_env({"http.https://github.com/.extraheader": f"AUTHORIZATION: basic {basic}"})
        _git("-C", str(checkout), "push", "-q", f"https://github.com/{fork}.git", f"HEAD:refs/heads/{branch}", **auth)

    def open_draft_pr(self, fork: str, branch: str, title: str, body: str) -> str:
        head = f"{fork.partition('/')[0]}:{branch}"
        return self.repo.create_pull(base=self.repo.default_branch, head=head, title=title, body=body, draft=True).html_url


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
