import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from functools import cache, cached_property
from pathlib import Path
from typing import Self

import yaml

ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".git").exists())
INTEGRATION_DIR = ROOT / "tests" / "ci" / "integration"
FAILURE_FILE = ROOT / "integration-failure.txt"
OMNIBUS = ROOT / ".github" / "workflows" / "integration_omnibus.yml"
AI_CONFIG = ROOT / ".github" / "workflows" / "ai-config.json"

WORK = Path(os.environ.get("AUTOFIX_WORK", ROOT / "autofix-poc-work")).resolve()
MODEL = os.environ.get("AUTOFIX_MODEL", "us.anthropic.claude-sonnet-5")
REGION = os.environ.get("AWS_REGION") or json.loads(AI_CONFIG.read_text())["aws_region"]
CONTEXT_1M = os.environ.get("AUTOFIX_1M", "1") == "1"
FORK = os.environ.get("AUTOFIX_FORK", "prasden/pd-aws-lc")
BASE = os.environ.get("AUTOFIX_BASE", "main")
MAX_TURNS = int(os.environ.get("AUTOFIX_MAX_TURNS", 40))
MAX_TOKENS = int(os.environ.get("AUTOFIX_MAX_TOKENS", 400_000))
MAX_OUTPUT_TOKENS = int(os.environ.get("AUTOFIX_MAX_OUTPUT_TOKENS", 64_000))
TIMEOUT = int(os.environ.get("AUTOFIX_TIMEOUT", 900))
FOCUS = os.environ.get("AUTOFIX_FOCUS", "1") == "1"

_PATCH_TOKEN = re.compile(r"[a-z0-9_]+_patch")
_COMPILER_SETUP = re.compile(r"setup-(\S+)\.sh")


def sh(*args: str | Path, check: bool = True) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=check).stdout


def git(*args: str | Path, check: bool = True) -> str:
    return sh("git", "-C", str(ROOT), *args, check=check).strip()


@cache
def omnibus() -> dict:
    return yaml.safe_load(OMNIBUS.read_text())


def matrix() -> list[dict]:
    return omnibus()["jobs"]["integrations"]["strategy"]["matrix"]["include"]


def _has_image(image: str) -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


@dataclass
class Target:
    integration: str
    version: str = ""
    repos: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> Self:
        head, repos = "", []
        for line in filter(str.strip, text.splitlines()):
            key, eq, value = line.partition("=")
            if not eq:
                head = head or line
            elif key == "commit":
                url, _, ref = value.strip().partition(" ")
                if url and ref:
                    repos.append((url, ref.strip()))
        if not head:
            raise ValueError("integration-failure.txt has no target line")
        integration, _, version = head.partition("\t")
        return cls(integration.strip(), version.strip(), repos)

    @classmethod
    def load(cls, path: Path = FAILURE_FILE) -> Self:
        if not path.exists():
            raise SystemExit(f"no {path.name} — run prep/ingest/run first")
        return cls.parse(path.read_text())

    @classmethod
    def from_run(cls, run: str) -> Self:
        script, *args = run.split()
        integration = Path(script).name.removeprefix("run_").removesuffix("_integration.sh")
        return cls(integration, args[0] if args else "")

    def save(self, path: Path = FAILURE_FILE) -> None:
        commits = [f"commit={url} {sha}" for url, sha in self.repos]
        path.write_text("\n".join([f"{self.integration}\t{self.version}", *commits]) + "\n")

    @property
    def name(self) -> str:
        name = f"{self.integration}-{self.version}" if self.version else self.integration
        return name.replace("/", "-")

    @property
    def runner(self) -> Path:
        return INTEGRATION_DIR / f"run_{self.integration}_integration.sh"

    @cached_property
    def patch_dirs(self) -> list[Path]:
        tokens = set(_PATCH_TOKEN.findall(self.runner.read_text())) if self.runner.exists() else set()
        dirs = [INTEGRATION_DIR / token for token in sorted(tokens)]
        return [d for d in dirs if d.is_dir()] or [INTEGRATION_DIR / f"{self.integration}_patch"]

    @property
    def editable(self) -> list[Path]:
        return [*self.patch_dirs, self.runner]

    def dir(self, kind: str) -> Path:
        path = WORK / self.name / kind
        path.mkdir(parents=True, exist_ok=True)
        return path

    @cached_property
    def ci_job(self) -> dict:
        for entry in matrix():
            other = Target.from_run(entry["run"])
            if other.integration == self.integration and self.version in ("", other.version):
                return entry
        steps = omnibus()["jobs"].get(self.integration, {}).get("steps", [])
        docker = next((s["with"] for s in steps if self.runner.name in s.get("with", {}).get("run", "")), {})
        compiler = _COMPILER_SETUP.search(docker.get("run", ""))
        return {"image": docker.get("image", "").rpartition("aws-lc/")[2], "compiler": compiler and compiler[1]}

    @cached_property
    def image(self) -> str:
        if override := os.environ.get("AUTOFIX_VERIFY_IMAGE"):
            return override
        image = self.ci_job.get("image", "")
        if not image or "{{" in image:
            image = "ubuntu:22.04"
        if registry := os.environ.get("AUTOFIX_ECR_REGISTRY"):
            return f"{registry}/aws-lc/{image}"
        return f"aws-lc/{image}" if _has_image(f"aws-lc/{image}") else image

    @property
    def command(self) -> str:
        command = f"bash {self.runner.relative_to(ROOT).as_posix()} {self.version}".rstrip()
        if compiler := self.ci_job.get("compiler"):
            setup = f"/opt/compiler-env/setup-{compiler}.sh"
            command = f"if [ -f {setup} ]; then source {setup}; fi; {command}"
        return command

    def run_in_docker(self, env: dict[str, str] | None = None) -> tuple[int, str]:
        env_args = [arg for key, value in (env or {}).items() for arg in ("-e", f"{key}={value}")]
        args = ["docker", "run", "--rm", "-v", f"{ROOT}:/aws-lc", "-w", "/aws-lc", *env_args, self.image,
                "timeout", "-k", "10", str(TIMEOUT * 4), "bash", "-c", self.command]
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        except FileNotFoundError:
            return 127, "docker not found — install Docker to run the real Linux runner"
        lines = []
        with proc:
            for line in proc.stdout:
                lines.append(line)
                out(f"  | {line.rstrip()}")
        return proc.returncode, "".join(lines)


@dataclass
class _Progress:
    step: str = "starting"
    step_started: float = field(default_factory=time.monotonic)
    last_output: float = field(default_factory=time.monotonic)
    mid_line: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_progress = _Progress()


def out(text: str, end: str = "\n") -> None:
    with _progress.lock:
        sys.stdout.write(text + end)
        sys.stdout.flush()
        _progress.last_output, _progress.mid_line = time.monotonic(), not (text + end).endswith("\n")


def _line(msg: str) -> None:
    out(("\n" if _progress.mid_line else "") + f"[autofix] {msg}")


def step(msg: str) -> None:
    _progress.step, _progress.step_started = msg, time.monotonic()
    _line(msg)


def start_heartbeat(interval: float = 10) -> None:
    def beat() -> None:
        while True:
            time.sleep(1)
            if time.monotonic() - _progress.last_output >= interval:
                _line(f"... still {_progress.step} ({int(time.monotonic() - _progress.step_started)}s)")

    threading.Thread(target=beat, daemon=True).start()
