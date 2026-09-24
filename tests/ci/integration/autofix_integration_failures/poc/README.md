# Strands autofix — local proof of concept

Repairs a broken AWS-LC integration test with a Strands agent, runs an adversarial
reviewer + regex secret scan, and opens a **draft PR on your fork**. Works on **real**
aws-lc failures and is **generic** — no per-integration config.

## Flow

1. Point at a real failure (`pr`, `prep`, or `run`).
2. `reason` — a Strands agent reads the runner + logs, clones the downstream repo at
   the failing ref, finds rejected hunks, and edits the **patch and/or the runner
   script**. A reviewer agent + regex secret scan gate the result.
3. `verify` — run the **real runner in Docker** to confirm green.
4. `open-pr` — draft PR on your fork with the corrected files.

## Not strands_shell (why)

The CI design sandboxes the agent in `strands_shell` (a Linux kernel sandbox). It is
Linux-only, so on macOS this PoC uses discrete, allowlisted Strands tools (`agent.py`):
read-only git, `patch --dry-run`, bounded read, a write confined to the patch dirs +
runner, and a delete confined to the patch dirs. This matches the security guidance (Mit 5/6). Swap in `strands_shell` for CI.

## Setup

```bash
cd tests/ci/integration/autofix_integration_failures/poc
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export AWS_PROFILE=<bedrock-account>
export AWS_REGION=us-west-2
export AUTOFIX_MODEL=us.anthropic.claude-sonnet-5   # default; 1M context on (AUTOFIX_1M=1)
```

Needs: Bedrock access to `AUTOFIX_MODEL`, `gh` (authed), `git`/`patch`; Docker + an image
with build deps for `verify`/`run` (a local `aws-lc/<image>` build is picked up automatically;
a plain `ubuntu:22.04` lacks cmake/gcc/autotools).

## Run (real failure → PR on your fork)

Generic — any failing integration. `pr` finds the failing integrations on a PR, repairs
each with the real runner in Docker, and opens a draft PR per green, reviewer-approved fix.

```bash
python -m autofix_poc pr <number> --repo aws/aws-lc --open-pr

# or step by step:  prep <integration> [version]  |  run <integration> [version]

# repair. AUTOFIX_VERIFY=1 runs the REAL runner in Docker each attempt and feeds its
# log back to the agent until green (needs Docker). Otherwise a fast dry-run loop that
# only checks the patch applies.
AUTOFIX_VERIFY=1 python -m autofix_poc reason

python -m autofix_poc verify              # re-run the real runner in Docker
python -m autofix_poc open-pr             # dry-run: shows the plan
python -m autofix_poc open-pr --yes       # push + open the draft PR on your fork
```

## Config (env)

| var | default | meaning |
|-----|---------|---------|
| `AUTOFIX_MODEL` | `us.anthropic.claude-sonnet-5` | Bedrock model id |
| `AUTOFIX_1M` | `1` | send the 1M-context beta flag (set `0` to disable) |
| `AWS_REGION` | `us-west-2` | Bedrock region |
| `AUTOFIX_FORK` | `prasden/pd-aws-lc` | PR target repo (your fork) |
| `AUTOFIX_BASE` | `main` | PR base branch |
| `AUTOFIX_VERIFY` | unset | `1` = run the real runner in Docker each attempt |
| `AUTOFIX_VERIFY_IMAGE` | auto | override the image; default comes from the omnibus job (e.g. openssh → `amazonlinux:2023`, ruby → `ubuntu:24.04`), preferring a local `aws-lc/<image>` |
| `AUTOFIX_ECR_REGISTRY` | unset | use prebuilt `<registry>/aws-lc/<image>` (with build deps) instead of the bare base image |
| `AUTOFIX_FOCUS` | `1` | on each try, run only the tests that failed in CI (openssh, ruby). Set `0` for the full runner |
| `AUTOFIX_MAX_TURNS` / `AUTOFIX_MAX_TOKENS` / `AUTOFIX_TIMEOUT` / `AUTOFIX_MAX_TRIES` | 40 / 400000 / 900 / 2(5) | agent caps |

## Artifacts (`autofix-poc-work/<name>/out/`)

`description-of-changes.md`, `changes.diff`, `review-verdict.json`, `run-summary.json`
(tokens, cost, duration, tries, tool calls, secrets, tests_green), `run-report.md`,
`transcript.md`, plus `runner-<n>.log` in `../logs/`.

## Fork CI note

Opening the PR on your fork does **not** make GitHub's integration-omnibus run there —
that needs the CodeBuild/ECR CI infra (CDK) provisioned in your account. Local `verify`
(the real runner in Docker) is the green signal here.
