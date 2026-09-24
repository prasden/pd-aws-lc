import json
import shlex
import subprocess
import threading
from itertools import count
from pathlib import Path

from pydantic import BaseModel, Field
from strands import Agent, tool
from strands.hooks import BeforeToolCallEvent
from strands.models import BedrockModel

from . import core
from .core import INTEGRATION_DIR, Target

PROMPTS = Path(__file__).parent / "prompts"
SYSTEM_PROMPT = (PROMPTS / "system.md").read_text().strip()

_GIT_SUBCMDS = {"clone", "checkout", "log", "show", "diff", "rev-parse", "status", "ls-files"}
_GIT_DENY = {"config", "push", "remote", "fetch", "pull", "submodule", "-c", "--exec-path"}
_CLONE_ALLOW = ("file://", "https://github.com/")
_MAX_TOOL_OUTPUT = 40000


class ReviewVerdict(BaseModel):
    safe: bool = Field(description="true only if the diff is a minimal, secret-free patch fix")
    findings: list[str] = Field(default_factory=list, description="anything suspicious found")
    rationale: str = Field(default="", description="one-sentence justification")


class Toolbox:
    def __init__(self, target: Target, description: Path):
        self.work = target.dir("src")
        self.patch_dirs = target.patch_dirs
        self.readable = [self.work, target.dir("logs"), INTEGRATION_DIR]
        self.writable = [*target.editable, description]
        self.tools = [self.run_git, self.patch_dry_run, self.read_file, self.list_dir, self.write_file, self.delete_file]

    def _guard(self, path: str, roots: list[Path]) -> Path:
        resolved = (self.work / path).resolve()
        if not any(resolved.is_relative_to(root) for root in roots):
            raise ValueError(f"{resolved} is outside the allowed paths: {', '.join(map(str, roots))}")
        return resolved

    @tool
    def run_git(self, args: str, cwd: str = "") -> str:
        """Run a read-only git command (or a clone from github/file://).

        args: git arguments, e.g. "checkout 3.13" or "clone file:///tmp/demo demo".
        cwd: directory under the workspace to run in (default: workspace root).
        """
        parts = shlex.split(args)
        if not parts or parts[0] not in _GIT_SUBCMDS:
            raise ValueError(f"git subcommand not allowed: {parts[:1]}")
        if _GIT_DENY.intersection(parts):
            raise ValueError("git command contains a denied token")
        if parts[0] == "clone":
            url = next((t for t in parts[1:] if "://" in t or t.startswith("file:")), "")
            if not url.startswith(_CLONE_ALLOW):
                raise ValueError(f"clone URL not allowlisted: {url!r}")
        workdir = self._guard(cwd, [self.work])
        workdir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["git", *parts], cwd=workdir, capture_output=True, text=True, timeout=300)
        return (result.stdout + result.stderr)[-8000:]

    @tool
    def patch_dry_run(self, patch_file: str, target_dir: str, strip: int = 1) -> str:
        """Run `patch --dry-run -pSTRIP` for patch_file against target_dir.

        "Hunk #N FAILED" means a reject; offsets and fuzz are acceptable.
        """
        patch = self._guard(patch_file, self.readable)
        directory = self._guard(target_dir, [self.work])
        result = subprocess.run(["patch", "--dry-run", f"-p{strip}", "-i", patch, "-d", directory],
                                capture_output=True, text=True, timeout=120)
        return f"exit={result.returncode}\n{result.stdout}{result.stderr}"

    @tool
    def read_file(self, path: str, start: int = 1, end: int = 0) -> str:
        """Read a file under the workspace, logs, or integration dir, with line
        numbers. Optional 1-based line range [start, end] (end=0 means EOF).
        Output stops at 40000 characters. Read a line range for large files."""
        lines = self._guard(path, self.readable).read_text(errors="replace").splitlines()
        text = "\n".join(f"{n}\t{line}" for n, line in enumerate(lines, 1) if n >= start and (not end or n <= end))
        return text[:_MAX_TOOL_OUTPUT]

    @tool
    def list_dir(self, path: str = "") -> str:
        """List one directory under the workspace, logs, or integration dir.
        Subdirectories end with "/"."""
        root = self._guard(path, self.readable)
        return "\n".join(sorted(f"{p.name}/" if p.is_dir() else p.name for p in root.iterdir()))

    @tool
    def write_file(self, path: str, content: str) -> str:
        """Create or overwrite a file. Allowed only for the patch files and the
        runner script — the editable surface. Use this to replace broken hunks,
        add a new patch, or adjust the runner (build flags, refs, test invocation)."""
        target = self._guard(path, self.writable)
        if not content.strip():
            raise ValueError("empty content: use delete_file for a patch that is no longer needed")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {len(content)} bytes to {target}"

    @tool
    def delete_file(self, path: str) -> str:
        """Delete a patch file that upstream already applied. Allowed only in the
        patch dirs. Also remove the runner step that applies it. Never write an
        empty patch in place of a deletion."""
        target = self._guard(path, self.patch_dirs)
        if not target.is_file():
            raise ValueError(f"not a file: {target}")
        target.unlink()
        return f"deleted {target}"


def _stream_text(**event) -> None:
    if text := event.get("data"):
        core.out(text, end="")


def model() -> BedrockModel:
    beta = {"additional_request_fields": {"anthropic_beta": ["context-1m-2025-08-07"]}} if core.CONTEXT_1M else {}
    return BedrockModel(model_id=core.MODEL, region_name=core.REGION, max_tokens=core.MAX_OUTPUT_TOKENS, **beta)


def repair_agent(bedrock: BedrockModel, toolbox: Toolbox) -> Agent:
    calls = count(1)

    def log_tool_call(event: BeforeToolCallEvent) -> None:
        core.step(f"agent tool call #{next(calls)}: {event.tool_use['name']}")

    return Agent(model=bedrock, tools=toolbox.tools, system_prompt=SYSTEM_PROMPT,
                 callback_handler=_stream_text, hooks=[log_tool_call])


def invoke(agent: Agent, prompt: str):
    cancel = threading.Event()
    timer = threading.Timer(core.TIMEOUT, cancel.set)
    timer.daemon = True
    timer.start()
    try:
        return agent(prompt, limits={"turns": core.MAX_TURNS, "total_tokens": core.MAX_TOKENS}, cancel_signal=cancel)
    finally:
        timer.cancel()


def review(bedrock: BedrockModel, diff: str, context: str) -> ReviewVerdict:
    reviewer = Agent(model=bedrock, system_prompt=(PROMPTS / "reviewer.md").read_text(), callback_handler=None)
    prompt = f"{context}\n\n## Diff to review\n\n{diff}\n\nReturn the verdict."
    return reviewer(prompt, structured_output_model=ReviewVerdict).structured_output


def _render(block: dict) -> str:
    if "toolUse" in block:
        return f"**[tool] {block['toolUse']['name']}**\n```\n{json.dumps(block['toolUse']['input'], indent=2)}\n```"
    if "toolResult" in block:
        return "```\n" + "\n".join(c.get("text", "") for c in block["toolResult"]["content"])[:2000] + "\n```"
    if "reasoningContent" in block:
        return f"> _thinking_ {block['reasoningContent'].get('reasoningText', {}).get('text', '')}"
    return block.get("text", "")


def transcript(messages: list[dict]) -> str:
    return "\n\n".join(f"### {m['role']}\n\n" + "\n\n".join(map(_render, m["content"])) for m in messages)
