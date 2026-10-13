import json
import logging
import re
import unicodedata
from pathlib import Path

from pydantic import BaseModel

from .agent import Agent, read_ci_logs, run_integration
from .git import Git, Repo
from .sandbox import Sandbox, VerifyResult
from .target import Environment, IntegrationTarget

_PROMPTS = Path(__file__).parent / "prompts"
_LICENSE = ("By submitting this pull request, I confirm that my contribution is made under the terms of the "
            "Apache 2.0 license and the ISC license.")
_SECRET_PATTERNS = (
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"aws_secret_access_key\s*=\s*\S+", re.I),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\b(?:Bearer|Authorization:)\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"https?://\S*\.(?:amazon|a2z|aws\.dev|corp)\b\S*"),
)
_URL = re.compile(r"https?://[^\s<>()\[\]`'\"]+")
_INVISIBLE_CATEGORIES = {"Cc", "Cf", "Cn", "Zl", "Zp"}


class ReviewVerdict(BaseModel):
    safe: bool
    findings: list[str] = []


# Build the job matrix from integration-failure.txt reports: one reason job per target on x86_64 when it failed there,
# and one verify job for its other failed arch. Example: ruby failing on both arches reasons on x86_64, verifies aarch64.
def recognize(reports: list[str], awslc: Repo) -> dict[str, list[dict]]:
    matrix = {"reason": [], "verify": []}
    for target in _parse_reports(reports, awslc):
        first, *others = sorted(target.arches, key=lambda arch: arch != "x86_64")
        for stage, arch in [("reason", first), *(("verify", arch) for arch in others)]:
            matrix[stage].append({"name": target.dir_name, "arch": arch, "size": target.size(arch),
                                  "target": target.model_dump(mode="json")})
    return matrix


# Group integration-failure.txt reports into one target per integration and version, skipping any bad report.
def _parse_reports(reports: list[str], awslc: Repo) -> list[IntegrationTarget]:
    targets: dict[tuple[str, str], IntegrationTarget] = {}
    for report in sorted(reports):
        try:
            head, *lines = report.splitlines()
            name, version = head.split("\t")
            repos = [Repo(*line.removeprefix("commit=").split()) for line in lines if line.startswith("commit=")]
            env = Environment.model_validate_json(lines[-1].removeprefix("job="))
            target = targets.setdefault((name, version), IntegrationTarget(
                name=name, version=version, awslc=awslc, repos=repos, environments=[]))
        except (ValueError, IndexError) as error:
            logging.warning("skipping a bad integration-failure.txt: %s", error)
            continue
        target.environments.append(env)
    return list(targets.values())


class Harness:
    def __init__(self, work_dir: Path, target: IntegrationTarget):
        self.work_dir = work_dir.resolve()
        self.target = target
        self.out = self.work_dir / "out"
        self.out.mkdir(parents=True, exist_ok=True)

    @property
    def diff(self) -> str:
        return (self.out / "changes.diff").read_text()

    @property
    def description(self) -> str:
        return (self.out / "description.md").read_text()

    # Let the agent repair the target with run_integration as its feedback loop, then record whether the final patch passes.
    def reason(self, registry: str) -> VerifyResult:
        sandbox = Sandbox(self.work_dir, self.target, registry)
        instructions = (_PROMPTS / "agent.md").read_text().format(
            name=self.target.name,
            version=self.target.version,
            root=str(sandbox.root),
            awslc=f"{self.target.awslc.web} at `{self.target.awslc.sha}`",
            jobs=", ".join(f"`out/logs/{env.job_id}.log` ({self.target.awslc.web}/runs/{env.job_id})"
                           for env in self.target.environments if env.job_id),
            repos=", ".join(f"`src/{repo.name}` ({repo.web} at `{repo.sha}`)" for repo in self.target.repos),
            editable=", ".join(f"`aws-lc/{path}`" for path in sandbox.editable),
        )
        with sandbox.agent_container() as container:
            agent = Agent(instructions,
                          sandbox=container,
                          tools=[run_integration],
                          builtin_tools=["shell", "read", "write", "edit"])
            final_message = agent("Start by reading the logs.", invocation_state={"sandbox": sandbox})

        (self.out / "changes.diff").write_text(sandbox.awslc.diff(sandbox.editable))
        (self.out / "description.md").write_text(str(final_message))
        (self.out / "transcript.json").write_text(json.dumps(agent.messages, indent=2, default=str))
        (self.out / "metrics-reason.json").write_text(json.dumps(final_message.metrics.get_summary(), indent=2, default=str))
        return self.verify(sandbox)

    # Replay the fix in changes.diff on this runner's arch and save the result, after the agent or on a second arch.
    def verify(self, sandbox: Sandbox) -> VerifyResult:
        result = sandbox.replay(self.diff)
        (self.out / f"verify-{sandbox.jobs[0].arch}.json").write_text(result.model_dump_json(indent=2))
        return result

    # Judge a verified fix with the fixed rules, then with a reviewer model that sees only the diff, description, and logs.
    def review(self) -> ReviewVerdict:
        verified = all(VerifyResult.model_validate_json((self.out / f"verify-{arch}.json").read_text()).passed
                       for arch in self.target.arches)
        if not verified:
            verdict = ReviewVerdict(safe=False, findings=["The fix did not pass every failed CI job."])
        elif not self._follows_rules():
            verdict = ReviewVerdict(safe=False, findings=["The diff breaks a fixed rule: scope, secrets, or hidden text."])
        else:
            logs = "\n\n".join(log.read_text() for log in sorted((self.out / "logs").glob("*.log")))
            message = f"<diff>\n{self.diff}\n</diff>\n\n<description>\n{self.description}\n</description>"
            reviewer = Agent((_PROMPTS / "review.md").read_text(), tools=[read_ci_logs])
            result = reviewer(message, invocation_state={"logs": logs}, structured_output_model=ReviewVerdict)
            (self.out / "metrics-review.json").write_text(json.dumps(result.metrics.get_summary(), indent=2, default=str))
            verdict = result.structured_output
        (self.out / "review.json").write_text(verdict.model_dump_json(indent=2))
        return verdict

    # Commit a fix the review found safe onto the tested aws-lc commit, and write the PR body.
    def resolve(self, checkout: Git) -> dict[str, str]:
        if not ReviewVerdict.model_validate_json((self.out / "review.json").read_text()).safe:
            raise PermissionError(f"{self.target.dir_name}: the fix was not reviewed safe")

        branch = f"autofix/{self.target.dir_name}"
        title = f"Fix the {self.target.dir_name} integration patch"
        checkout.apply(self.diff)
        checkout.commit(branch, title)

        (self.out / "pr.md").write_text(f"{self.description.strip()}\n\n{_LICENSE}\n")
        return {"branch": branch, "title": f"[autofix] {title}"}

    # Match a GitHub permalink to a commit, a file at the tested commit, or a failed CI job of the target's repos.
    # Example: https://github.com/aws/aws-lc/runs/115492504715 or <tpm2-tss URL>/commit/<40-hex SHA>.
    @property
    def _permalink(self) -> re.Pattern:
        awslc = self.target.awslc
        files = (rf"{re.escape(repo.web)}/(?:commit/[0-9a-f]{{40}}|blob/{repo.sha}/[\w./-]+(?:#L\d+(?:-L\d+)?)?)"
                 for repo in [awslc, *self.target.repos])
        jobs = "|".join(str(env.job_id) for env in self.target.environments if env.job_id)
        return re.compile("|".join([*files, rf"{re.escape(awslc.web)}/runs/(?:{jobs})"]))

    # Check for secrets leakage, hidden text, links that are not permalinks, or edits outside the integration/ directory.
    # A hardening for defense in depth. 
    def _follows_rules(self) -> bool:
        diff = self.diff
        if not diff:
            return False
        runner = self.target.runner
        added = [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++ ")]
        text = "\n".join([*added, self.description])
        return (
            all(path == runner or (path.suffix == ".patch" and path.is_relative_to(runner.parent))
                for path in Git.changed_paths(diff))
            and all(self._permalink.fullmatch(url.rstrip(".,:;")) for url in _URL.findall(self.description))
            and not any(pattern.search(text) for pattern in _SECRET_PATTERNS)
            and not any(unicodedata.category(char) in _INVISIBLE_CATEGORIES and char not in "\t\n\r" for char in text)
        )
