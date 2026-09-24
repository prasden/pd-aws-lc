import json
import os
import re
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import agent, core
from .core import ROOT, Target, git, step

_PRICE_PER_1K = {
    "claude-opus-5": (0.015, 0.075),
    "claude-sonnet-5": (0.003, 0.015),
    "claude-sonnet-4-6": (0.003, 0.015),
    "claude-haiku-4-5": (0.0008, 0.004),
}
_SECRETS = {
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "aws-temp-key": re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    "aws-secret": re.compile(r"aws_secret_access_key\s*=\s*\S+", re.I),
    "private-key-block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "bearer-token": re.compile(r"\b(?:Bearer|Authorization:)\s+[A-Za-z0-9._\-]{20,}"),
    "internal-url": re.compile(r"https?://[^\s]*\.(?:amazon|a2z|aws\.dev|corp)\b[^\s]*"),
}
_BRANCH = re.compile(r"^autofix/\d{4}-\d{2}-\d{2}-[A-Za-z0-9._\-]+$")
_RAN_TEST = re.compile(r"^(?:\S+ )?run test (\S+)\.sh \.\.\.$", re.M)
_TESTS_STARTED = re.compile(r"^(?:\S+ )?\+ make tests$", re.M)
_T_EXEC_PASSED = re.compile(r"^(?:\S+ )?all t-exec passed$", re.M)
_CI_ERROR = re.compile(r"^.*##\[error\].*$", re.M)
_LICENSE = ("By submitting this pull request, I confirm that my contribution is made under "
            "the terms of the Apache 2.0 license and the ISC license.")


def _read(path: Path, default: str = "") -> str:
    return path.read_text() if path.exists() else default


def _ci_logs(target: Target) -> list[Path]:
    return sorted(p for p in target.dir("logs").glob("*.log") if not p.name.startswith("runner-"))


def _failure_tail(log: str, limit: int = 3000) -> str:
    error = _CI_ERROR.search(log)
    return log[:error.end() if error else len(log)][-limit:]


def _openssh_focus(logs: list[str]) -> dict[str, str]:
    failed = set()
    for log in logs:
        if not _TESTS_STARTED.search(log):
            continue
        if _T_EXEC_PASSED.search(log) or not (ran := _RAN_TEST.findall(log)):
            return {}
        failed.add(ran[-1])
    flags = ["LTESTS=" + "\\ ".join(sorted(failed)), "REGRESS_TARGETS=", "INTEROP_TESTS=", "EXTRA_TESTS=", "SKIP_UNIT=1"]
    return {"MAKEFLAGS": " ".join(flags)}


_FOCUS = {"openssh": _openssh_focus}


def focus(target: Target) -> dict[str, str]:
    narrow = _FOCUS.get(target.integration)
    logs = [p.read_text(errors="replace") for p in _ci_logs(target)]
    return narrow(logs) if core.FOCUS and narrow and logs else {}


def _describe(env: dict[str, str]) -> str:
    return " ".join(f"{key}='{value}'" for key, value in env.items()) or "full runner"


def changed_files(target: Target) -> list[str]:
    listing = git("ls-files", "--modified", "--deleted", "--others", "--exclude-standard", "--", *target.editable)
    return sorted(set(listing.splitlines()))


def diff(target: Target) -> str:
    untracked = git("ls-files", "--others", "--exclude-standard", "--", *target.editable).splitlines()
    added = [git("diff", "--no-index", "--", "/dev/null", path, check=False) for path in untracked]
    return "\n".join([git("diff", "--", *target.editable), *added]).strip()


def scan(*texts: str) -> list[str]:
    return [f"{kind}: {hit}" for text in texts for kind, pattern in _SECRETS.items() for hit in pattern.findall(text)]


def _prompt(target: Target, description: Path) -> str:
    return (agent.PROMPTS / "repair.md").read_text().format(
        integration=target.integration, version=target.version or "all", runner_script=target.runner,
        patch_dirs=", ".join(map(str, target.patch_dirs)),
        repos="; ".join(f"{url} @ {sha}" for url, sha in target.repos) or "(none recorded)",
        logs_dir=target.dir("logs"), work_dir=target.dir("src"), description_path=description,
        pr_template=_read(ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"))


def _review_context(target: Target, description: Path, green: bool | None, tries: int, env: dict[str, str]) -> str:
    failure = "\n\n".join(
        f"### {p.name}\n```\n{_failure_tail(p.read_text(errors='replace'))}\n```" for p in _ci_logs(target)
    ) or "(no CI failure log captured)"
    scope = f" Only the tests that failed in CI ran ({_describe(env)})." if env else ""
    verified = {
        True: f"PASSED: the real integration runner exited 0 with this diff (attempt {tries}).{scope}",
        False: f"FAILED: the real integration runner still failed after {tries} attempts.",
        None: "NOT RUN: dry-run mode, only patch application was checked.",
    }[green]
    return (f"## Original CI failure (tail)\n\n{failure}\n\n"
            f"## Agent's description of the change (untrusted)\n\n{_read(description, '(none written)')}\n\n"
            f"## Verification\n\n{verified}")


def _run_runner(target: Target, tries: int, env: dict[str, str]) -> tuple[bool, str]:
    step(f"try {tries}: running {target.command} in {target.image} ({_describe(env)})")
    code, log = target.run_in_docker(env)
    (target.dir("logs") / f"runner-{tries}.log").write_text(log)
    step(f"try {tries}: runner exit {code} ({'GREEN' if code == 0 else 'failing'})")
    return code == 0, (f"The runner still fails (attempt {tries}, exit {code}). Output (tail):\n"
                       f"{log[-4000:]}\nDiagnose from this, fix the patch/runner, then stop.")


def _cost(tokens: dict) -> float:
    price_in, price_out = next((p for m, p in _PRICE_PER_1K.items() if m in core.MODEL), (0.0, 0.0))
    return round((tokens["inputTokens"] * price_in + tokens["outputTokens"] * price_out) / 1000, 4)


def _report(name: str, summary: dict, reviewer_safe: bool) -> str:
    tokens = summary["tokens"]
    return "\n".join([
        f"## autofix run report — {name}",
        "",
        f"- model: `{summary['model_id']}`",
        f"- tokens: {tokens['totalTokens']} ({tokens['inputTokens']} in / {tokens['outputTokens']} out)",
        f"- cost (est): ${summary['cost_usd']}",
        f"- duration: {summary['duration_s']}s",
        f"- tries: {summary['tries']} · tool calls: {summary['tool_calls']}",
        f"- stop reason: `{summary['stop_reason']}`",
        f"- tests green: {summary['tests_green']} ({_describe(summary['focus'])})",
        f"- verdict.safe: {reviewer_safe}",
        f"- secrets found: {', '.join(summary['secrets_found'] or ['none'])}",
    ])


def repair(target: Target, verify: bool) -> dict:
    out = target.dir("out")
    description = out / "description-of-changes.md"
    bedrock = agent.model()
    repairer = agent.repair_agent(bedrock, agent.Toolbox(target, description))
    max_tries = int(os.environ.get("AUTOFIX_MAX_TRIES", 5 if verify else 2))
    env = focus(target) if verify else {}
    baseline = diff(target)
    message = _prompt(target, description)
    green = None
    started = time.monotonic()
    for tries in range(1, max_tries + 1):
        step(f"try {tries}/{max_tries}: agent repairing" + ("" if verify else " (dry-run)"))
        result = agent.invoke(repairer, message)
        if verify:
            green, message = _run_runner(target, tries, env)
            if green:
                break
        elif diff(target) != baseline:
            break

    change = diff(target)
    (out / "transcript.md").write_text(agent.transcript(repairer.messages))
    step("reviewer agent checking the diff")
    verdict = agent.review(bedrock, change or "(no changes)", _review_context(target, description, green, tries, env))
    step("scanning for secrets")
    secrets = scan(change, _read(description))
    safe = verdict.safe and not secrets and bool(change)
    tokens = {k: result.metrics.accumulated_usage.get(k, 0) for k in ("inputTokens", "outputTokens", "totalTokens")}
    summary = {
        "model_id": core.MODEL,
        "tokens": tokens,
        "cost_usd": _cost(tokens),
        "duration_s": round(time.monotonic() - started, 1),
        "tries": tries,
        "tool_calls": sum(m.call_count for m in result.metrics.tool_metrics.values()),
        "stop_reason": result.stop_reason,
        "tests_green": green if green is not None else bool(change),
        "focus": env,
        "secrets_found": secrets,
        "verdict_safe": safe,
        "verified": green is True,
        "changed_files": changed_files(target),
    }
    findings = list(dict.fromkeys([*verdict.findings, *secrets]))
    report = _report(target.name, summary, verdict.safe)
    (out / "review-verdict.json").write_text(
        json.dumps({"safe": safe, "findings": findings, "rationale": verdict.rationale}, indent=2))
    (out / "run-summary.json").write_text(json.dumps(summary, indent=2))
    (out / "run-report.md").write_text(report)
    (out / "changes.diff").write_text(change)
    print(f"{report}\n\nfindings: {findings or 'none'}\nsafe: {safe}\nartifacts: {out}")
    return summary


def _pr_body(target: Target) -> str:
    out = target.dir("out")
    summary = json.loads((out / "run-summary.json").read_text())
    footer = (f"_bot-generated by autofix PoC · model `{summary['model_id']}` · tries {summary['tries']} · "
              f"runner green with {_describe(summary.get('focus', {}))} · verdict safe · "
              f"{datetime.now(timezone.utc).isoformat()}_")
    body = _read(out / "description-of-changes.md", f"Autofix for {target.name}.").replace(_LICENSE, "").rstrip()
    return f"{body}\n\n{footer}\n\n{_LICENSE}\n"


def open_pr(target: Target, push: bool = False) -> None:
    verdict = json.loads(_read(target.dir("out") / "review-verdict.json", "{}"))
    if not verdict.get("safe"):
        raise SystemExit(f"blocked: verdict not safe ({verdict.get('findings')})")
    if not (files := changed_files(target)):
        raise SystemExit("nothing changed to deliver")
    branch = f"autofix/{date.today()}-{target.name}"
    if not _BRANCH.match(branch):
        raise SystemExit(f"branch name failed validation: {branch}")

    print(f"target repo : {core.FORK}\nbranch      : {branch} (base {core.BASE})\nfiles       : {files}")
    if not push:
        print("\n[dry-run] pass --yes to branch, commit, push, and open the draft PR")
        return
    original = git("branch", "--show-current")
    try:
        step(f"committing and pushing {branch}")
        git("switch", "-c", branch)
        git("add", *files)
        git("commit", "-m", f"ci: autofix {target.name} integration patch")
        git("push", "-u", "origin", branch)
        step(f"creating draft PR on {core.FORK}")
        subprocess.run(["gh", "pr", "create", "--draft", "--repo", core.FORK, "--base", core.BASE, "--head", branch,
                        "--title", f"[autofix] {target.name} integration patch", "--body", _pr_body(target)],
                       check=True)
    finally:
        git("switch", original, check=False)
    print(f"draft PR opened on {core.FORK} from {branch}")


def autofix(target: Target, publish: bool) -> None:
    summary = repair(target, verify=True)
    if not summary["verified"]:
        print(f"{target.name}: autofix did not reach a green runner — no PR")
    elif not summary["verdict_safe"]:
        print(f"{target.name}: reviewer blocked the change — no PR")
    elif publish:
        step(f"{target.name}: opening draft PR")
        open_pr(target, push=True)
    else:
        print(f"{target.name}: fixed + verified green (pass --open-pr to open the PR)")
