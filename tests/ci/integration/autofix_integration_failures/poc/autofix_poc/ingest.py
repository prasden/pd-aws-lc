import json
import re
import subprocess
import tempfile
from pathlib import Path

from .core import FAILURE_FILE, Target, matrix, omnibus, sh

_CLONE_URL = re.compile(r"git\s+clone[^|;&]*?((?:https?|git)://[^\s'\"]+)")
_JOB_ID = re.compile(r"/job/(\d+)")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def prep(integration: str, version: str = "") -> Target:
    target = Target(integration, version)
    if not target.runner.exists():
        raise SystemExit(f"no runner: {target.runner}")
    urls = dict.fromkeys(url for url in _CLONE_URL.findall(target.runner.read_text()) if "$" not in url)
    target.repos = [(url, version or "HEAD") for url in urls]
    target.save()
    print(f"prepared {FAILURE_FILE} for {integration} {version}".rstrip())
    for url in urls:
        print(f"  repo: {url} (ref resolved from runner)")
    if not urls:
        print("  note: no static clone URL found in runner — agent will read the runner")
    return target


def _gh_list(path: str, key: str) -> list[dict]:
    return [json.loads(line) for line in sh("gh", "api", path, "--paginate", "-q", f".{key}[]").splitlines()
            if line.strip()]


def _save_job_log(repo: str, job_id: str, path: Path) -> None:
    endpoint = f"/repos/{repo}/actions/jobs/{job_id}/logs"
    for flags in (["--allow-escape-sequences"], []):
        try:
            raw = sh("gh", "api", *flags, endpoint)
            break
        except subprocess.CalledProcessError:
            continue
    else:
        print(f"  warning: could not fetch the CI log for job {job_id}")
        return
    path.write_text("".join(c for c in _ANSI.sub("", raw) if c.isprintable() or c in "\t\n"))


def job_target(job: str) -> Target | None:
    for entry in matrix():
        suffix = "-allow-failure" if entry.get("allow_failure") else ""
        if job == f"{entry['name']}-{entry['arch']}{suffix}":
            return Target.from_run(entry["run"])
    integration, _, rest = job.partition("-")
    if integration in omnibus()["jobs"] and rest:
        return Target(integration, rest.split("-")[0])
    return None


def discover_pr(pr: str, repo: str) -> list[Target]:
    head = sh("gh", "pr", "view", pr, "--repo", repo, "--json", "headRefOid", "-q", ".headRefOid").strip()
    failed: dict[str, tuple[Target, list[dict]]] = {}
    for run in _gh_list(f"/repos/{repo}/commits/{head}/check-runs", "check_runs"):
        if run.get("conclusion") == "failure" and (found := job_target(run.get("name", ""))):
            failed.setdefault(found.name, (found, []))[1].append(run)

    targets = []
    for found, runs in failed.values():
        target = prep(found.integration, found.version)
        for run in runs:
            if job_id := _JOB_ID.search(run.get("details_url", "")):
                _save_job_log(repo, job_id.group(1), target.dir("logs") / f"{run['name']}.log")
        targets.append(target)
    print(f"detected failing: {', '.join(t.name for t in targets)}" if targets
          else "no failing integration detected on the PR")
    return targets


def ingest(run_id: str, repo: str, integration: str | None = None) -> Target:
    tmp = Path(tempfile.mkdtemp())
    try:
        subprocess.run(["gh", "run", "download", run_id, "--repo", repo,
                        "--pattern", "integration-failure-*", "--dir", str(tmp)], check=True)
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"gh run download failed (scheduled runs only): {e}")
    files = sorted(tmp.rglob("integration-failure.txt"))
    if not files:
        raise SystemExit("no integration-failure.txt artifacts (was it a scheduled run?)")
    target = next((t for t in map(Target.load, files) if integration in (None, t.integration)), None)
    if target is None:
        raise SystemExit(f"no failing target matched integration={integration}")

    target.save()
    prefix = target.integration.replace("_", "-")
    try:
        jobs = _gh_list(f"/repos/{repo}/actions/runs/{run_id}/jobs", "jobs")
    except subprocess.CalledProcessError:
        jobs = []
    for job in jobs:
        if job.get("conclusion") == "failure" and job.get("name", "").startswith(prefix):
            _save_job_log(repo, job["id"], target.dir("logs") / f"{job['id']}.log")
    print(f"ingested {target.integration} {target.version} → {FAILURE_FILE}".rstrip())
    return target
