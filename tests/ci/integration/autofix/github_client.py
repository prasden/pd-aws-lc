import io
import os
import re
import zipfile
from pathlib import Path

import requests
from github import Auth, Github
from github.WorkflowJob import WorkflowJob

from . import git
from .git import Repo
from .integration import IntegrationTarget

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean_log(log: bytes) -> str:
    text = _ANSI_ESCAPE.sub("", log.decode(errors="replace"))
    return "".join(char for char in text if char.isprintable() or char in "\t\n")


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
        return {job.id: _clean_log(self._download(f"{job.url}/logs")) for job in self._failed_jobs(run_id, target)}

    # Pick the target's failed jobs by the ids in its reports, or by job name when the reports predate job ids.
    # Example: ruby master matches ruby-master-x86_64 and ruby-master-fips-x86_64 but not ruby-3.4-x86_64.
    def _failed_jobs(self, run_id: int, target: IntegrationTarget) -> list[WorkflowJob]:
        failed = [job for job in self.repo.get_workflow_run(run_id).jobs() if job.conclusion == "failure"]
        if job_ids := {env.job_id for env in target.environments if env.job_id}:
            return [job for job in failed if job.id in job_ids]
        prefix = target.name.replace("_", "-")
        return ([job for job in failed if job.name.startswith(f"{prefix}-{target.version}")]
                or [job for job in failed if job.name.startswith(prefix)])

    def push(self, checkout: Path, fork: str, branch: str) -> None:
        git.push(checkout, f"https://github.com/{fork}.git", branch, self.token)

    def open_draft_pr(self, fork: str, branch: str, title: str, body: str) -> str:
        head = f"{fork.partition('/')[0]}:{branch}"
        return self.repo.create_pull(base=self.repo.default_branch, head=head, title=title, body=body, draft=True).html_url
