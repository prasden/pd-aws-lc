import json
import os
import re
import threading
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel
from strands.agent import AgentResult
from strands.tools.executors import SequentialToolExecutor
from strands.types.agent import Limits
from strands_harness import create_harness

from .git import Git
from .sandbox import Sandbox, ReplayResult
from .target import IntegrationTarget

_AI_CONFIG = json.loads((Path(__file__).parents[4] / ".github/workflows/ai-config.json").read_text())
_MODEL = f"bedrock/{_AI_CONFIG['opus']}"
_LIMITS = Limits(turns=150, total_tokens=6_000_000)
_TIMEOUT = 45 * 60
_REASON_TIMEOUT = 60 * 60
_PROMPTS = Path(__file__).parent / "prompts"
_REVIEW_LOG_TAIL = 100_000
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
_INVISIBLE_CATEGORIES = {"Cc", "Cf", "Cn", "Zl", "Zp"}


class ReviewVerdict(BaseModel):
    safe: bool
    findings: list[str] = []


class Harness:
    def __init__(self, work_dir: Path, target: IntegrationTarget):
        self.work_dir = work_dir.resolve()
        self.target = target
        self.out = self.work_dir / "out"
        self.out.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> Self:
        return cls(Path(os.environ["WORK_DIR"]), IntegrationTarget.model_validate_json(os.environ["TARGET"]))

    @staticmethod
    def _run_agent(instructions: str, message: str, timeout: int, call: dict[str, Any], **options: Any) -> AgentResult:
        agent = create_harness(**{
            "model": _MODEL,
            "instructions": instructions,
            "builtin_tools": [],
            "builtin_plugins": [],
            "context_manager": "auto",
            "session": False,
            "memory": False,
            "skills": False,
            "background_tasks": False,
            "tool_executor": SequentialToolExecutor(),
        } | options)
        timer = threading.Timer(timeout, agent.cancel)
        timer.start()
        try:
            return agent(message, limits=_LIMITS, **call)
        finally:
            timer.cancel()

    def reason(self, registry: str) -> ReplayResult:
        """Let the agent fix the target, then replay its final diff and record whether it passes."""
        sandbox = Sandbox(self.work_dir, self.target, registry)
        instructions = (_PROMPTS / "agent.md").read_text().format(
            name=self.target.name,
            version=self.target.version,
            root=str(sandbox.root),
            repos=", ".join(f"`src/{repo.name}` ({repo.url} at {repo.sha})" for repo in self.target.repos),
            editable=", ".join(f"`aws-lc/{path}`" for path in sandbox.editable),
        )
        with sandbox.agent_container() as container:
            final_message = self._run_agent(
                instructions,
                "Start by reading the logs.",
                _REASON_TIMEOUT,
                {},
                sandbox=container,
                tools=[sandbox.run_integration, sandbox.run],
                builtin_tools=["read", "write", "edit"],
            )
        if final_message.stop_reason != "end_turn":
            raise RuntimeError(f"{self.target.dir_name}: the agent stopped early ({final_message.stop_reason})")

        diff = sandbox.awslc.diff(sandbox.editable)
        (self.out / "changes.diff").write_text(diff)
        (self.out / "description.md").write_text(str(final_message))

        result = sandbox.replay(diff) if diff else ReplayResult(passed=False, report="The agent changed nothing.")
        (self.out / "replay.json").write_text(result.model_dump_json(indent=2))
        return result

    def review(self, run_url: str) -> ReviewVerdict:
        """Check a fix's diff to see if it follow rules, then by a new reviewer model that checks
        the diff and description.

        Write pr.md only for a safe fix. The resolve GHA opens a PR only when pr.md exists.
        """
        replay = ReplayResult.model_validate_json((self.out / "replay.json").read_text())
        diff, description = (self.out / "changes.diff").read_text(), (self.out / "description.md").read_text()

        if not replay.passed:
            verdict = ReviewVerdict(safe=False, findings=["The fix did not pass every failed CI job."])
        elif not self._follows_rules(diff, description):
            verdict = ReviewVerdict(safe=False, findings=["The diff breaks a fixed rule: scope, secrets, or hidden text."])
        else:
            logs = "\n\n".join(log.read_text()[-_REVIEW_LOG_TAIL:]
                               for log in sorted((self.out / "logs").glob("*.log")))
            message = (f"<logs>\n{logs}\n</logs>\n\n<diff>\n{diff}\n</diff>\n\n"
                       f"<description>\n{description}\n</description>")

            verdict = self._run_agent(
                (_PROMPTS / "review.md").read_text(),
                message,
                _TIMEOUT,
                {"structured_output_model": ReviewVerdict},
            ).structured_output

        (self.out / "review.json").write_text(verdict.model_dump_json(indent=2))
        if verdict.safe:
            bot = f"Generated by autofix with `{_MODEL}` at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC in {run_url}."
            (self.out / "pr.md").write_text(f"{description.strip()}\n\n{bot}\n\n{_LICENSE}\n")
        return verdict

    def _follows_rules(self, diff: str, description: str) -> bool:
        """Return whether the diff edits only the runner and its .patch files, with no secrets or hidden text."""
        if not diff:
            return False
        runner = self.target.runner
        added = [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++ ")]
        text = "\n".join([*added, description])
        return (
            all(path == runner or (path.suffix == ".patch" and path.is_relative_to(runner.parent))
                for path in Git.changed_paths(diff))
            and not any(pattern.search(text) for pattern in _SECRET_PATTERNS)
            and not any(unicodedata.category(char) in _INVISIBLE_CATEGORIES and char not in "\t\n\r" for char in text)
        )
