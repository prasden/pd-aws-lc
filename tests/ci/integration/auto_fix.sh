#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0 OR ISC
#
# auto_fix.sh — Drafts patch fixes for AWS-LC integration tests that broke
# due to downstream changes.
#
# Triggered by .github/workflows/autofix_integration_omnibus.yml when the
# nightly integration_omnibus run fails. For each failed integration, asks
# Claude (via Bedrock) to read the existing patch, reproduce the failure,
# author a new patch, validate it, and open a draft PR.
#
# Usage: ./auto_fix.sh <integration_omnibus_run_id>
#
# Required environment:
#   GH_TOKEN                          GitHub token (from Secrets Manager)
#   GITHUB_REPO_OWNER, GITHUB_REPO_NAME
#   CLAUDE_CODE_USE_BEDROCK=1
#   AWS_REGION, ANTHROPIC_DEFAULT_*_MODEL
#
# Exits 0 if all autofix attempts completed (regardless of per-integration
# success) — failures are surfaced via the missing draft PRs and the SIM
# ticket cut by the alarm. Non-zero exit only on infrastructure errors
# (e.g., Claude install missing, GitHub API unreachable).

set -exuo pipefail

RUN_ID="${1:?Usage: $0 <integration_omnibus_run_id>}"
REPO="${GITHUB_REPO_OWNER}/${GITHUB_REPO_NAME}"
MAX_ATTEMPTS=3
CLAUDE_TIMEOUT_SECONDS=1200

SRC_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
INTEGRATION_DIR="${SRC_ROOT}/tests/ci/integration"
WORK_ROOT="${SRC_ROOT}/.autofix-workspace"
mkdir -p "${WORK_ROOT}"

# Configure git identity and auth for any branches/commits/pushes the
# autofix flow makes. GH_TOKEN must already be exported.
if [ -z "${GH_TOKEN:-}" ]; then
  echo "GH_TOKEN is not set; cannot push branches or open PRs."
  exit 1
fi
git -C "${SRC_ROOT}" config user.email "aws-lc-ci@amazon.com"
git -C "${SRC_ROOT}" config user.name "AWS-LC CI Autofix"
gh auth setup-git

# 1. Discover failed jobs from the failing run.
echo "Fetching failed jobs from run ${RUN_ID} in ${REPO}..."
FAILED_JOBS_FILE="${WORK_ROOT}/failed-jobs.txt"
gh run view "${RUN_ID}" --repo "${REPO}" --json jobs --jq \
  '.jobs[] | select(.conclusion == "failure" and .name != "report-failures" and .name != "autofix") | .name' \
  > "${FAILED_JOBS_FILE}"

if [ ! -s "${FAILED_JOBS_FILE}" ]; then
  echo "No failed integration jobs found in run ${RUN_ID}; nothing to autofix."
  exit 0
fi

echo "Failed jobs:"
cat "${FAILED_JOBS_FILE}"

# 2. Map each failed job to its integration patch dir.
# Patch dirs are named <name>_patch/. Convert underscores to hyphens to
# get the expected job-name prefix, then longest-prefix-match against the
# failing job name. Examples:
#   pq_tls_patch    -> "pq-tls"    matches "pq-tls-x86_64"
#   python_patch    -> "python"    matches "python-3.13-...-x86_64"
#   tpm2_tss_patch  -> "tpm2-tss"  matches "tpm2-tss-x86_64"
#
# Aliases handle matrix names that don't directly map to a patch dir name
# (e.g., matrix uses "postgresql" but the patch dir is "postgres_patch").
declare -A NAME_ALIASES=(
  ["postgresql"]="postgres"
)

PATCH_PREFIXES_FILE="${WORK_ROOT}/patch-prefixes.txt"
{
  for d in "${INTEGRATION_DIR}"/*_patch; do
    [ -d "${d}" ] || continue
    name=$(basename "${d}")
    integration="${name%_patch}"
    prefix="${integration//_/-}"
    echo "${#prefix} ${prefix} ${integration}"
  done
  for alias_name in "${!NAME_ALIASES[@]}"; do
    integration="${NAME_ALIASES[$alias_name]}"
    echo "${#alias_name} ${alias_name} ${integration}"
  done
} | sort -rn > "${PATCH_PREFIXES_FILE}"  # longest prefix wins

INTEGRATIONS_FILE="${WORK_ROOT}/integrations.txt"
: > "${INTEGRATIONS_FILE}"
while IFS= read -r job; do
  [ -z "${job}" ] && continue
  while IFS= read -r line; do
    prefix=$(echo "${line}" | awk '{print $2}')
    integration=$(echo "${line}" | awk '{print $3}')
    case "${job}" in
      "${prefix}-"*)
        echo "${integration}" >> "${INTEGRATIONS_FILE}"
        break
        ;;
    esac
  done < "${PATCH_PREFIXES_FILE}"
done < "${FAILED_JOBS_FILE}"

sort -u -o "${INTEGRATIONS_FILE}" "${INTEGRATIONS_FILE}"

if [ ! -s "${INTEGRATIONS_FILE}" ]; then
  echo "No failed jobs map to a known patch directory; nothing to autofix."
  exit 0
fi

echo "Integrations to autofix:"
cat "${INTEGRATIONS_FILE}"

# 3. For each integration, attempt up to MAX_ATTEMPTS autofix invocations.
overall_status=0
while IFS= read -r integration; do
  [ -z "${integration}" ] && continue

  patch_dir="${INTEGRATION_DIR}/${integration}_patch"
  runner_script="${INTEGRATION_DIR}/run_${integration}_integration.sh"

  if [ ! -d "${patch_dir}" ] || [ ! -f "${runner_script}" ]; then
    echo "Skipping ${integration}: patch dir or runner script not found."
    continue
  fi

  # Start each integration from a clean main branch so its commits don't
  # stack on top of a previous integration's autofix.
  branch_name="autofix/${integration}-${RUN_ID}"
  git -C "${SRC_ROOT}" checkout main
  git -C "${SRC_ROOT}" branch -D "${branch_name}" 2>/dev/null || true
  git -C "${SRC_ROOT}" checkout -b "${branch_name}"

  # Collect this integration's failing job names + log tails for context.
  jobs_for_int=$(grep "^${integration}\b\|^${integration}-" "${FAILED_JOBS_FILE}" || true)
  logs_dir="${WORK_ROOT}/${integration}/logs"
  mkdir -p "${logs_dir}"
  while IFS= read -r job_name; do
    [ -z "${job_name}" ] && continue
    safe_name=$(echo "${job_name}" | tr '/' '_')
    job_id=$(gh run view "${RUN_ID}" --repo "${REPO}" --json jobs \
      --jq ".jobs[] | select(.name == \"${job_name}\") | .databaseId" | head -n1)
    if [ -n "${job_id}" ]; then
      gh api "/repos/${REPO}/actions/jobs/${job_id}/logs" \
        > "${logs_dir}/${safe_name}.log" 2>/dev/null \
        || echo "Could not fetch logs for ${job_name}"
      # Keep only the last 200 lines per job to stay within Claude's context budget
      tail -n 200 "${logs_dir}/${safe_name}.log" > "${logs_dir}/${safe_name}.tail" || true
    fi
  done <<< "${jobs_for_int}"

  # Run Claude. Up to MAX_ATTEMPTS retries on infra/timeout errors.
  attempt=0
  rc=1
  while [ "${attempt}" -lt "${MAX_ATTEMPTS}" ] && [ "${rc}" -ne 0 ]; do
    attempt=$((attempt + 1))
    echo "=== Autofix attempt ${attempt}/${MAX_ATTEMPTS} for ${integration} ==="

    prompt="You are auto-fixing a broken integration test patch in AWS-LC.

    The integration name is: ${integration}.
    The runner script: ${runner_script}
    The current patch directory (the patches that broke): ${patch_dir}
    The failing GHA run is at https://github.com/${REPO}/actions/runs/${RUN_ID}.
    Truncated logs from the failed jobs are in: ${logs_dir}
    The repository working copy is rooted at ${SRC_ROOT}. You are currently inside it on a fresh feature branch named ${branch_name} cut from main. Do not switch branches.
    
    Your task:
    1. Read the runner script and identify the downstream repository URL, the branch/tag it clones, and how the patches are applied (\`patch -p1\` is the standard).
    2. Clone the downstream repo into a scratch directory under ${WORK_ROOT}/${integration}/src.
    3. Apply the existing patches with \`patch --dry-run -p1\` and identify which hunks fail to apply.
    4. Read the failing source context in the cloned repo to understand what changed upstream.
    5. Author corrected patch file(s) that apply cleanly. Place them in ${patch_dir}, replacing the broken ones.
    6. Validate by running \`patch --dry-run -p1\` against a fresh clone — must succeed with no fuzz and no rejected hunks.
    7. Stage your changes (\`git add tests/ci/integration/${integration}_patch\`) and commit with message: 'autofix(${integration}): repair patch broken by downstream change (run ${RUN_ID})'.
    8. Push the branch and open a DRAFT pull request via the gh CLI: \`gh pr create --draft --title \"autofix(${integration}): repair patch\" --body \"...\"\`. The body should reference the failing run, list which patch files changed, and note that this is an unverified autofix that needs maintainer review.
    
    If you cannot produce a clean patch after best-effort attempts, do NOT open a PR. Instead, print 'AUTOFIX_FAILED: ${integration}' to stdout and exit. The on-call will review the failed integration manually.
    
    Use the gh CLI for any GitHub API calls. GH_TOKEN is already set."

    set +e
    timeout "${CLAUDE_TIMEOUT_SECONDS}" claude -p "${prompt}" \
      --allowedTools "Read,Glob,Grep,Bash,Edit,Write,Agent,WebFetch" \
      --output-format text
    rc=$?
    set -e

    if [ "${rc}" -eq 124 ]; then
      echo "Claude timed out for ${integration} on attempt ${attempt}."
    elif [ "${rc}" -ne 0 ]; then
      echo "Claude exited with code ${rc} for ${integration} on attempt ${attempt}."
    fi
  done

  if [ "${rc}" -ne 0 ]; then
    echo "Autofix gave up on ${integration} after ${MAX_ATTEMPTS} attempts."
    overall_status=1
  fi
done < "${INTEGRATIONS_FILE}"

echo "Autofix run complete."
exit "${overall_status}"
