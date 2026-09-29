import os
import re
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from strands.sandbox.docker import DockerSandbox
from strands_harness import create_harness

from .agent import (MODEL, RETRY_PROMPT, REVIEW_PROMPT, REVIEW_TASK_PROMPT, SYSTEM_PROMPT, TASK_PROMPT,
                    ReviewVerdict, describe_results, run_environments, run_integration)
from .git import GitClient, GitHubClient
from .integration import IntegrationTarget

_MAX_TRIES = 3
_AGENT_TIMEOUT = 15 * 60
_LOG_TAIL = 3000
_SECRET_PATTERNS = {
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "AWS secret key": re.compile(r"aws_secret_access_key\s*=\s*\S+", re.I),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "bearer token": re.compile(r"\b(?:Bearer|Authorization:)\s+[A-Za-z0-9._\-]{20,}"),
    "internal URL": re.compile(r"https?://\S*\.(?:amazon|a2z|aws\.dev|corp)\b\S*"),
}
# Match characters that hide text from people but not from models, except tab, newline, and carriage return.
# Example: zero-width spaces, bidi overrides, and U+E0000 tag characters that spell out hidden instructions.
_INVISIBLE_RANGES = [(0x00, 0x08), (0x0B, 0x0C), (0x0E, 0x1F), (0x7F, 0x9F), (0xAD, 0xAD), (0x200B, 0x200F),
                     (0x2028, 0x202E), (0x2060, 0x2064), (0x2066, 0x2069), (0xFEFF, 0xFEFF), (0xE0000, 0xE007F)]
_INVISIBLE_CHARACTERS = re.compile(
    "[" + "".join(f"{re.escape(chr(low))}-{re.escape(chr(high))}" for low, high in _INVISIBLE_RANGES) + "]")


class Harness:
    def __init__(self, work_dir: Path = Path("sandbox")):
        self.work_dir = work_dir

    def sandbox_dir(self, target: IntegrationTarget) -> Path:
        return (self.work_dir / target.dir_name).resolve()


    # -------- recognize -------------------
    def prepare_sandbox(self, github: GitHubClient, run_id: str) :
        git = GitClient()
        for target in github.failure_targets(run_id):
            sandbox = self.sandbox_dir(target)

            logs_dir = sandbox / "logs"
            logs_dir.mkdir(parents=True)

            for job_id, log in github.job_logs(target).items():
                (logs_dir / f"{job_id}.log").write_text(log)

            git.clone(target.awslc_repo, sandbox / "aws-lc")
            for integration_repo in target.integration_repos:
                git.clone(integration_repo, sandbox / "src" / integration_repo.name)


    # -------- reason -------------------
    def reason(self, target: IntegrationTarget, registry: str):
        sandbox_dir = self.sandbox_dir(target)
        awslc = sandbox_dir / "aws-lc"
        runner = target.runner(awslc)
        image = target.environments[0].image_ref(registry)

        patch_dirs = target.patch_dirs(awslc)
        writable = [*patch_dirs, runner]

        with self.agent_sandbox(image, sandbox_dir, writable) as sandbox:
            agent = create_harness(
                model=MODEL,
                instructions=SYSTEM_PROMPT,
                sandbox=sandbox,
                tools=[run_integration(target, awslc, registry)],
                builtin_tools=["shell", "read", "write", "edit"],
                builtin_plugins=[], session=False, memory=False, skills=False,
            )

            prompt = TASK_PROMPT.format(
                name=target.name,
                version=target.version or "(none)",
                sandbox_dir=sandbox_dir,
                repos=", ".join(f"`{sandbox_dir / 'src' / repo.name}` ({repo.url} at {repo.sha})"
                                for repo in target.integration_repos) or "(none recorded)",
                runner=runner,
                patch_dirs=", ".join(f"`{patch_dir}`" for patch_dir in patch_dirs),
            )
            for _ in range(_MAX_TRIES):
                timer = threading.Timer(_AGENT_TIMEOUT, agent.cancel)
                timer.start()
                try:
                    final_message = agent(prompt)
                finally:
                    timer.cancel()

                results = run_environments(target, awslc, registry)
                if all(result.passed for result in results):
                    break
                prompt = RETRY_PROMPT.format(results=describe_results(results))

        out_dir = sandbox_dir / "out"
        out_dir.mkdir(exist_ok=True)
        (out_dir / "description.md").write_text(str(final_message))


    # -------- review -------------------
    def review(self, target: IntegrationTarget) -> ReviewVerdict:
        sandbox_dir = self.sandbox_dir(target)
        awslc = sandbox_dir / "aws-lc"
        runner = target.runner(awslc)
        out_dir = sandbox_dir / "out"

        allowed_paths = [*target.patch_dirs(awslc), runner]
        git = GitClient()
        diff = git.diff(awslc, allowed_paths)
        changed_names = git.diff(awslc, allowed_paths, "--name-only").splitlines()
        changed_files = [awslc / name for name in changed_names]
        description = (out_dir / "description.md").read_text()
        logs = "\n\n".join(log.read_text()[-_LOG_TAIL:] for log in sorted((sandbox_dir / "logs").glob("*.log")))

        reviewer = create_harness(
            model=MODEL,
            system_prompt=REVIEW_PROMPT,
            builtin_tools=[], builtin_plugins=[], session=False, memory=False, skills=False,
            callback_handler=None,
        )
        verdict = reviewer(
            REVIEW_TASK_PROMPT.format(logs=logs, diff=diff),
            structured_output_model=ReviewVerdict,
        ).structured_output

        checks = [
            *(f"{kind} found" for kind, pattern in _SECRET_PATTERNS.items() if pattern.search(diff + description)),
            *(f"invisible or control character in the {name}"
              for name, text in (("diff", diff), ("description", description)) if _INVISIBLE_CHARACTERS.search(text)),
            *(f"not a patch file or the runner: {path.relative_to(awslc)}"
              for path in changed_files if path != runner and path.suffix != ".patch"),
            *([] if diff else ["the diff is empty"]),
        ]
        final = ReviewVerdict(
            safe=verdict.safe and not checks,
            findings=[*verdict.findings, *checks],
            rationale=verdict.rationale,
        )

        (out_dir / "changes.diff").write_text(diff)
        (out_dir / "review.json").write_text(final.model_dump_json(indent=2))
        return final


    # Run a container that sees the target's sandbox folder read-only, except the writable paths, with no network or creds.
    @contextmanager
    def agent_sandbox( self, image: str, sandbox_dir: Path, writable: list[Path]) -> Iterator[DockerSandbox]:

        name = f"autofix-agent-{sandbox_dir.name}"
        volumes = [f"--volume={sandbox_dir}:{sandbox_dir}:ro"]

        for path in writable:
            if not path.exists():
                path.mkdir(parents=True)
            volumes.append(f"--volume={path}:{path}:rw")

        subprocess.run([
            "docker", "run", "--detach", "--rm",
            "--name", name,
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--user={os.getuid()}:{os.getgid()}",
            *volumes,
            image,
            "sleep", "infinity",
        ], check=True, capture_output=True)

        try:
            yield DockerSandbox(name, working_dir=str(sandbox_dir))
        finally:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True)
