import asyncio
import json
import re
import threading
from datetime import date
from functools import partial
from itertools import count
from pathlib import Path

from pydantic import BaseModel
from strands import tool
from strands.hooks import AfterInvocationEvent
from strands.tools.executors import SequentialToolExecutor
from strands.types.agent import Limits
from strands_harness import create_harness

from . import git
from .git import GitHubClient
from .integration import IntegrationTarget, Sandbox

_AI_CONFIG = json.loads((Path(__file__).parents[4] / ".github/workflows/ai-config.json").read_text())
_MAX_TRIES = 3
_AGENT_TIMEOUT = 45 * 60
_AGENT_LIMITS = Limits(turns=150, total_tokens=6_000_000)
_REVIEW_LOG_TAIL = 3000
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
# Match characters that hide text from people but not from models, except tab, newline, and carriage return.
# Example: zero-width spaces, bidi overrides, and U+E0000 tag characters that spell out hidden instructions.
_INVISIBLE_RANGES = [(0x00, 0x08), (0x0B, 0x0C), (0x0E, 0x1F), (0x7F, 0x9F), (0xAD, 0xAD), (0x200B, 0x200F),
                     (0x2028, 0x202E), (0x2060, 0x2064), (0x2066, 0x2069), (0xFEFF, 0xFEFF), (0xE0000, 0xE007F)]
_INVISIBLE_CHARACTERS = re.compile(
    "[" + "".join(f"{re.escape(chr(low))}-{re.escape(chr(high))}" for low, high in _INVISIBLE_RANGES) + "]")
# Build a harness with no plugins, memory, sessions, skills, or background tasks, running one tool at a time.
# Example: two run_integration calls in one turn run one after the other instead of racing for one CI container name.
_bare_harness = partial(create_harness, model=f"bedrock/{_AI_CONFIG['opus']}", builtin_plugins=[], session=False,
                        memory=False, skills=False, background_tasks=False, tool_executor=SequentialToolExecutor())


def _prompt(name: str) -> str:
    return (Path(__file__).parent / "prompts" / f"{name}.md").read_text()


# Apply the fixed rules no reviewer verdict can override: a non-empty diff of only patches and the runner, no secrets,
# and no hidden text. Example: a diff that touches CMakeLists.txt or holds a zero-width space fails.
def _follows_rules(diff: str, description: str, changed_files: list[Path], runner: Path) -> bool:
    text = diff + description
    has_secret = any(pattern.search(text) for pattern in _SECRET_PATTERNS)
    hides_text = _INVISIBLE_CHARACTERS.search(text)
    only_patches = all(path == runner or path.suffix == ".patch" for path in changed_files)
    return bool(diff) and only_patches and not has_secret and not hides_text


class ReviewVerdict(BaseModel):
    safe: bool


class VerifyResult(BaseModel):
    passed: bool


class Harness:
    def __init__(self, work_dir: Path):
        self.work_dir = work_dir.resolve()

    def sandbox(self, target: IntegrationTarget) -> Sandbox:
        return Sandbox(self.work_dir / target.dir_name)

    # Load a target saved by recognize, refusing a name that does not match the target it names.
    # Example: load("ruby-master") reads ruby-master/target.json, and load("../x") raises ValueError.
    def load(self, dir_name: str) -> IntegrationTarget:
        target = IntegrationTarget.load(self.work_dir / dir_name / "target.json")
        if target.dir_name != dir_name:
            raise ValueError(f"target.json in {dir_name!r} names {target.dir_name!r}")
        return target

    def recognize(self, github: GitHubClient, run_id: int) -> list[str]:
        targets = github.failure_targets(run_id)
        for target in targets:
            sandbox = self.sandbox(target)
            sandbox.logs.mkdir(parents=True)
            target.save(sandbox.root / "target.json")
            for job_id, log in github.job_logs(run_id, target).items():
                (sandbox.logs / f"{job_id}.log").write_text(log)
            git.clone(target.awslc_repo, sandbox.awslc)
            for repo in target.integration_repos:
                git.clone(repo, sandbox.src / repo.name)
        return [target.dir_name for target in targets]

    def reason(self, target: IntegrationTarget, registry: str) -> ReviewVerdict:
        sandbox = self.sandbox(target)
        tries = count(1)

        @tool
        async def run_integration() -> str:
            """Run every failed CI environment with the current patches and return each result and log tail."""
            return (await target.run(sandbox, registry))[1]

        # Run the integration after each finished agent pass and resume the agent with the failure, up to three passes.
        # Example: a fix that still fails on aarch64 resumes the agent with the aarch64 log tail.
        async def rerun_until_green(event: AfterInvocationEvent) -> None:
            if event.result and event.result.stop_reason == "end_turn" and next(tries) < _MAX_TRIES:
                passed, report = await target.run(sandbox, registry)
                if not passed:
                    event.resume = _prompt("retry").format(results=report)

        for patch_dir in target.patch_dirs(sandbox.awslc):
            patch_dir.mkdir(parents=True, exist_ok=True)
        with target.agent_container(sandbox, registry).start() as container:
            agent = _bare_harness(
                instructions=_prompt("system"),
                sandbox=container,
                tools=[run_integration],
                hooks=[rerun_until_green],
                builtin_tools=["shell", "read", "write", "edit"],
            )
            prompt = _prompt("task").format(
                name=target.name,
                version=target.version or "(none)",
                sandbox_dir=sandbox.root,
                repos=", ".join(f"`{sandbox.src / repo.name}` ({repo.url} at {repo.sha})"
                                for repo in target.integration_repos) or "(none recorded)",
                runner=target.runner(sandbox.awslc),
                patch_dirs=", ".join(f"`{patch_dir}`" for patch_dir in target.patch_dirs(sandbox.awslc)),
            )
            deadline = threading.Event()
            timer = threading.Timer(_AGENT_TIMEOUT, deadline.set)
            timer.start()
            try:
                final_message = agent(prompt, limits=_AGENT_LIMITS, cancel_signal=deadline)
            finally:
                timer.cancel()

        sandbox.out.mkdir(exist_ok=True)
        (sandbox.out / "description.md").write_text(str(final_message))
        return self.review(target)

    def review(self, target: IntegrationTarget) -> ReviewVerdict:
        sandbox = self.sandbox(target)
        allowed_paths = target.writable(sandbox.awslc)
        diff = git.diff(sandbox.awslc, allowed_paths)
        description = (sandbox.out / "description.md").read_text()
        logs = "\n\n".join(log.read_text()[-_REVIEW_LOG_TAIL:] for log in sorted(sandbox.logs.glob("*.log")))

        reviewer = _bare_harness(system_prompt=_prompt("review"), builtin_tools=[], callback_handler=None)
        verdict = reviewer(
            _prompt("review_task").format(logs=logs, diff=diff, description=description),
            structured_output_model=ReviewVerdict,
        ).structured_output

        changed_files = git.changed_files(sandbox.awslc, allowed_paths)
        follows_rules = _follows_rules(diff, description, changed_files, target.runner(sandbox.awslc))
        final = ReviewVerdict(safe=verdict.safe and follows_rules)
        (sandbox.out / "changes.diff").write_text(diff)
        (sandbox.out / "review.json").write_text(final.model_dump_json(indent=2))
        return final

    def verify(self, target: IntegrationTarget, registry: str) -> VerifyResult:
        sandbox = self.sandbox(target)
        git.apply(sandbox.awslc, sandbox.out / "changes.diff")
        passed, report = asyncio.run(target.run(sandbox, registry))

        sandbox.verify.mkdir(exist_ok=True)
        (sandbox.verify / "report.log").write_text(report)
        result = VerifyResult(passed=passed)
        (sandbox.verify / "verify.json").write_text(result.model_dump_json(indent=2))
        return result

    # Commit the reviewed and verified fix onto the failing aws-lc commit, push it to the fork, and open a draft PR.
    # Example: ruby master opens autofix/2026-10-09-ruby-master, and an unverified fix raises PermissionError.
    def resolve(self, target: IntegrationTarget, github: GitHubClient, fork: str) -> str:
        sandbox = self.sandbox(target)
        diff = (sandbox.out / "changes.diff").read_text()
        description = (sandbox.out / "description.md").read_text()
        git.apply(sandbox.awslc, sandbox.out / "changes.diff")

        reviewed = ReviewVerdict.model_validate_json((sandbox.out / "review.json").read_text()).safe
        verified = VerifyResult.model_validate_json((sandbox.verify / "verify.json").read_text()).passed
        changed_files = git.changed_files(sandbox.awslc, [sandbox.awslc])
        if not (reviewed and verified and _follows_rules(diff, description, changed_files, target.runner(sandbox.awslc))):
            raise PermissionError(f"{target.dir_name}: the fix was not reviewed safe and verified")

        branch = f"autofix/{date.today()}-{target.dir_name}"
        title = f"Fix the {target.dir_name} integration patch"
        git.commit(sandbox.awslc, branch, title)
        git.unshallow(sandbox.awslc, target.awslc_repo)
        github.push(sandbox.awslc, fork, branch)
        return github.open_draft_pr(fork, branch, f"[autofix] {title}", f"{description.strip()}\n\n{_LICENSE}\n")
