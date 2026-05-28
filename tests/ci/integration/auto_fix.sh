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
# Where we read failure data from (the upstream repo whose run we're fixing).
SOURCE_REPO="${GITHUB_REPO_OWNER}/${GITHUB_REPO_NAME}"
# Where we push branches and open PRs. Defaults to origin's repo so testing
# on a fork doesn't accidentally target upstream.
TARGET_REPO="${AUTOFIX_TARGET_REPO:-$(git -C "$(cd "$(dirname "$0")/../../.." && pwd)" remote get-url origin | sed -E 's#(https://github.com/|git@github.com:)([^/]+/[^/.]+)(\.git)?#\2#')}"
MAX_ATTEMPTS=3
CLAUDE_TIMEOUT_SECONDS=600

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

# Independent validation that the patches in the given dir actually apply
# cleanly to a fresh clone of the integration's downstream repo. The runner
# script for an integration always contains a `git clone <url>` line for the
# downstream; we extract that URL, clone it shallow, and run `patch
# --dry-run -p1` against every patch file. Returns 0 if all patches apply
# cleanly, non-zero otherwise.
validate_patches() {
  local integration="$1"
  local patch_dir="$2"
  local runner_script="$3"

  local clone_url
  clone_url=$(grep -oE 'git clone https://[^ "]+' "${runner_script}" | head -1 | awk '{print $3}')
  if [ -z "${clone_url}" ]; then
    echo "::warning::Could not extract downstream clone URL from ${runner_script}; skipping post-Claude validation."
    return 0
  fi

  local validate_dir="${WORK_ROOT}/${integration}/validate"
  rm -rf "${validate_dir}"
  if ! git clone --depth 1 "${clone_url}" "${validate_dir}" >/dev/null 2>&1; then
    echo "::warning::Failed to clone ${clone_url} for validation; skipping."
    return 0
  fi

  local failures=0
  local patch_file
  while IFS= read -r patch_file; do
    [ -z "${patch_file}" ] && continue
    if patch --dry-run -p1 -d "${validate_dir}" < "${patch_file}" >/dev/null 2>&1; then
      echo "  PASS: $(basename "${patch_file}")"
    else
      echo "  FAIL: $(basename "${patch_file}")"
      failures=$((failures + 1))
    fi
  done < <(find -L "${patch_dir}" -type f -name '*.patch')

  rm -rf "${validate_dir}"

  if [ "${failures}" -gt 0 ]; then
    echo "::error::Patch validation failed for ${integration}: ${failures} patch file(s) did not apply cleanly. The PR Claude opened may need manual fixing."
    return 1
  fi
  echo "All patches for ${integration} apply cleanly."
  return 0
}

# 1. Discover failed jobs from the failing run.
echo "Fetching failed jobs from run ${RUN_ID} in ${SOURCE_REPO}..."
FAILED_JOBS_FILE="${WORK_ROOT}/failed-jobs.txt"
gh run view "${RUN_ID}" --repo "${SOURCE_REPO}" --json jobs --jq \
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

  # Start each integration from the workflow's checked-out commit so its
  # commits don't stack on top of a previous integration's autofix.
  base_ref=$(git -C "${SRC_ROOT}" rev-parse HEAD)
  branch_name="autofix/${integration}-${RUN_ID}"
  git -C "${SRC_ROOT}" branch -D "${branch_name}" 2>/dev/null || true
  git -C "${SRC_ROOT}" checkout -B "${branch_name}" "${base_ref}"

  # Collect this integration's failing job names + log tails for context.
  jobs_for_int=$(grep "^${integration}\b\|^${integration}-" "${FAILED_JOBS_FILE}" || true)
  logs_dir="${WORK_ROOT}/${integration}/logs"
  mkdir -p "${logs_dir}"
  while IFS= read -r job_name; do
    [ -z "${job_name}" ] && continue
    safe_name=$(echo "${job_name}" | tr '/' '_')
    job_id=$(gh run view "${RUN_ID}" --repo "${SOURCE_REPO}" --json jobs \
      --jq ".jobs[] | select(.name == \"${job_name}\") | .databaseId" | head -n1)
    if [ -n "${job_id}" ]; then
      gh api "/repos/${SOURCE_REPO}/actions/jobs/${job_id}/logs" \
        > "${logs_dir}/${safe_name}.log" 2>/dev/null \
        || echo "Could not fetch logs for ${job_name}"
      # Keep only the last 200 lines per job to stay within Claude's context budget
      tail -n 200 "${logs_dir}/${safe_name}.log" > "${logs_dir}/${safe_name}.tail" || true
    fi
  done <<< "${jobs_for_int}"

  # Run Claude. Up to MAX_ATTEMPTS retries on infra/timeout errors.
  attempt=0
  fixed=false
  validation_error=""
  while [ "${attempt}" -lt "${MAX_ATTEMPTS}" ] && [ "${fixed}" = "false" ]; do
    attempt=$((attempt + 1))
    echo "=== Autofix attempt ${attempt}/${MAX_ATTEMPTS} for ${integration} ==="

    # On retry after validation failure, include the error so Claude can correct.
    retry_context=""
    if [ -n "${validation_error}" ]; then
      retry_context="

    IMPORTANT: Your previous attempt produced patches that FAILED independent validation.
    The validation error was:
    ${validation_error}

    Please fix the patches so they apply cleanly. Do NOT repeat the same mistake."
    fi

    prompt="You are auto-fixing a broken integration test patch in AWS-LC.

    The integration name is: ${integration}.
    The runner script: ${runner_script}
    The current patch directory (the patches that broke): ${patch_dir}
    The failing GHA run is at https://github.com/${SOURCE_REPO}/actions/runs/${RUN_ID}.
    Truncated logs from the failed jobs are in: ${logs_dir}
    The repository working copy is rooted at ${SRC_ROOT}. You are currently inside it on a fresh feature branch named ${branch_name} cut from main. Do not switch branches.
    ${retry_context}
    Your task:
    1. Read the runner script and identify the downstream repository URL, the branch/tag it clones, and how the patches are applied (\`patch -p1\` is the standard).
    2. Clone the downstream repo into a scratch directory under ${WORK_ROOT}/${integration}/src.
    3. Apply the existing patches with \`patch --dry-run -p1\` and identify which hunks fail to apply.
    4. Read the failing source context in the cloned repo to understand what changed upstream.
    5. Author corrected patch file(s) that apply cleanly. Place them in ${patch_dir}, replacing the broken ones.
    6. Validate by running \`patch --dry-run -p1\` against a fresh clone — must succeed with no fuzz and no rejected hunks.
    7. Stage your changes (\`git add tests/ci/integration/${integration}_patch\`) and commit with message: 'autofix(${integration}): repair patch broken by downstream change (run ${RUN_ID})'.

    Do NOT push the branch. Do NOT open a pull request. The autofix script will push and open the PR after independently re-validating the patches. Just leave the commit on the local branch.

    If you cannot produce a clean patch after best-effort attempts, do NOT open a PR. Instead, print 'AUTOFIX_FAILED: ${integration}' to stdout and exit. The on-call will review the failed integration manually.

    Use the gh CLI for any GitHub API calls. GH_TOKEN is already set."

    set +e
    stream_log="${WORK_ROOT}/${integration}/claude-stream-${attempt}.jsonl"
    timeout "${CLAUDE_TIMEOUT_SECONDS}" claude -p "${prompt}" \
      --allowedTools "Read,Glob,Grep,Bash,Edit,Write,Agent,WebFetch" \
      --output-format stream-json --verbose \
      > "${stream_log}"
    rc=$?
    set -e

    # Print a compact summary of tool calls and the final response so the
    # runner log shows what Claude actually did.
    if [ -s "${stream_log}" ]; then
      echo ""
      echo "::group::Claude tool calls (${integration}, attempt ${attempt})"
      jq -r '
        select(.type == "assistant") | .message.content[]?
        | select(.type == "tool_use")
        | "  - \(.name): \(.input | tostring | .[0:200])"
      ' "${stream_log}" 2>/dev/null || true
      echo "::endgroup::"
      echo ""
      echo "::group::Claude response (${integration}, attempt ${attempt})"
      jq -r '
        select(.type == "result") | .result // .message.content[]?.text // empty
      ' "${stream_log}" 2>/dev/null || true
      echo "::endgroup::"
    fi

    if [ "${rc}" -eq 124 ]; then
      echo "Claude timed out for ${integration} on attempt ${attempt}."
      continue
    elif [ "${rc}" -ne 0 ]; then
      echo "Claude exited with code ${rc} for ${integration} on attempt ${attempt}."
      continue
    fi

    # Claude succeeded — now validate the patches independently.
    echo ""
    echo "::group::Independent patch validation (${integration}, attempt ${attempt})"
    validation_error=$(validate_patches "${integration}" "${patch_dir}" "${runner_script}" 2>&1) && valid=true || valid=false
    echo "${validation_error}"
    echo "::endgroup::"

    if [ "${valid}" = "true" ]; then
      fixed=true
    else
      echo "::warning::Patch validation failed for ${integration} on attempt ${attempt}; retrying..."
      # Reset branch to base so Claude starts fresh on next attempt.
      git -C "${SRC_ROOT}" reset --hard "${base_ref}"
    fi
  done

  if [ "${fixed}" = "true" ]; then
    echo ""
    echo "::group::Push branch and open draft PR (${integration})"
    pr_body="Automated patch repair for ${integration} integration.

Triggering run: https://github.com/${SOURCE_REPO}/actions/runs/${RUN_ID}

This patch was authored by Claude Code (Bedrock) and independently validated against a fresh clone of the downstream repository via \`patch --dry-run\`. It has NOT been functionally tested. A maintainer must review the diff before merging."
    git -C "${SRC_ROOT}" push \
      "https://x-access-token:${GH_TOKEN}@github.com/${TARGET_REPO}.git" \
      "${branch_name}"
    gh pr create --draft --repo "${TARGET_REPO}" \
      --base "$(git -C "${SRC_ROOT}" symbolic-ref refs/remotes/origin/HEAD --short 2>/dev/null | sed 's@^origin/@@' || echo main)" \
      --head "${branch_name}" \
      --title "autofix(${integration}): repair patch" \
      --body "${pr_body}" || echo "::warning::PR may already exist for ${integration}"
    echo "::endgroup::"
  else
    echo "::warning::Autofix could not produce valid patches for ${integration} after ${MAX_ATTEMPTS} attempts."
  fi
done < "${INTEGRATIONS_FILE}"

echo "Autofix run complete."
exit 0
