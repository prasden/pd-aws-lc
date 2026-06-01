#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0 OR ISC
#
# Drives the autofix loop for failed integration tests. Two modes:
#   discover  - read the (integration, version) targets emitted by failed
#               integration omnibus jobs and print them as a JSON array
#   fix       - ask Claude to attempt a fix and create a PR for a target.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"



setup() {
  RUN_ID="${1:?Usage: $0 <integration_omnibus_run_id>}"
  REPO="${GITHUB_REPO_OWNER}/${GITHUB_REPO_NAME}"
  INTEGRATION_DIR="${SRC_ROOT}/tests/ci/integration"
  WORK_ROOT="${SRC_ROOT}/.autofix-workspace"
  MAX_ATTEMPTS=3
  CLAUDE_TIMEOUT=900

  mkdir -p "${WORK_ROOT}"

  if [[ -z "${GH_TOKEN:-}" ]]; then
    echo "GH_TOKEN is not set; cannot push branches or open PRs."
    exit 1
  fi

  git -C "${SRC_ROOT}" config user.email "aws-lc-ci@amazon.com"
  git -C "${SRC_ROOT}" config user.name  "AWS-LC CI Autofix"
}


# Fetch the last 200 lines of each failed job's log into the given directory 
# to feed as extra context to Claude when it attempts a fix.
# Args: $1 = integration name, $2 = output directory
fetch_logs() {
  local integration="$1"
  local logs_dir="$2"
  local prefix="${integration//_/-}"

  mkdir -p "${logs_dir}"

  local job_id
  for job_id in $(gh api "/repos/${REPO}/actions/runs/${RUN_ID}/jobs" \
                    --paginate \
                    --jq ".jobs[]
                          | select(.conclusion == \"failure\" and (.name | startswith(\"${prefix}\")))
                          | .id")
  do
    gh api "/repos/${REPO}/actions/jobs/${job_id}/logs" \
      | tail -n 200 > "${logs_dir}/${job_id}.log" || true
  done
}

build_prompt() {
  local integration="$1"
  local version="$2"
  local patch_dir="$3"
  local runner_script="$4"
  local logs_dir="$5"
  local branch_name="$6"

  sed -e "s|INTEGRATION_PLACEHOLDER|${integration}|g" \
      -e "s|VERSION_PLACEHOLDER|${version:-all}|g" \
      -e "s|PATCH_DIR_PLACEHOLDER|${patch_dir}|g" \
      -e "s|RUNNER_SCRIPT_PLACEHOLDER|${runner_script}|g" \
      -e "s|LOGS_DIR_PLACEHOLDER|${logs_dir}|g" \
      -e "s|BRANCH_NAME_PLACEHOLDER|${branch_name}|g" \
      -e "s|FAILING_RUN_PLACEHOLDER|https://github.com/${REPO}/actions/runs/${RUN_ID}|g" \
      -e "s|SRC_ROOT_PLACEHOLDER|${SRC_ROOT}|g" \
      -e "s|WORK_ROOT_PLACEHOLDER|${WORK_ROOT}|g" \
      -e "s|RUN_ID_PLACEHOLDER|${RUN_ID}|g" \
      "${SCRIPT_DIR}/prompt.md"
}

run_claude() {
  local rc=0
  timeout "${CLAUDE_TIMEOUT}" claude -p "$1" \
    --allowedTools "Read,Glob,Grep,Bash,Edit,Write,Agent,WebFetch" \
    --verbose > "$2" || rc=$?

  case "${rc}" in
    0)   ;;
    124) echo "::warning::Claude timed out after ${CLAUDE_TIMEOUT}s." ;;
    *)   echo "::warning::Claude exited with code ${rc}." ;;
  esac
  return "${rc}"
}

# Push the fix branch and open the draft PR. If the branch already exists on
# the remote, deletes the branch and lets the next nightly run try again.
open_pr() {
  local target="$1"
  local branch_name="$2"
  local push_url="https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git"

  if git -C "${SRC_ROOT}" ls-remote --exit-code "${push_url}" "refs/heads/${branch_name}" >/dev/null 2>&1; then
    echo "Branch ${branch_name} already exists on ${REPO}; skipping push (existing PR is still open)."
    return
  fi

  git -C "${SRC_ROOT}" push "${push_url}" "${branch_name}"

  local pr_body
  pr_body="$(git -C "${SRC_ROOT}" log -1 --format=%b)

  ---
  This PR was drafted automatically by the nightly autofix workflow using Claude Code. A maintainer should review and run CI before merging.
  Triggered by: https://github.com/${REPO}/actions/runs/${RUN_ID}"

  gh pr create --draft --repo "${REPO}" \
    --head "${branch_name}" \
    --title "autofix(${target}): repair patch" \
    --body "${pr_body}"
}

# Run the full fix-and-PR loop for one (integration, version) target.
fix_target() {
  local integration="$1"
  local version="$2"
  local base_ref="$3"

  local patch_dir="${INTEGRATION_DIR}/${integration}_patch"
  local runner_script="${INTEGRATION_DIR}/run_${integration}_integration.sh"
  [[ -d "${patch_dir}" && -f "${runner_script}" ]] || return

  local target="${integration}${version:+-${version}}"
  local branch_name="autofix/${target}"
  local work_dir="${WORK_ROOT}/${target}"
  mkdir -p "${work_dir}"

  git -C "${SRC_ROOT}" checkout -B "${branch_name}" "${base_ref}"

  fetch_logs "${integration}" "${work_dir}/logs"

  local prompt attempt
  prompt=$(build_prompt "${integration}" "${version}" "${patch_dir}" \
                        "${runner_script}" "${work_dir}/logs" "${branch_name}")

  # Retry on transient Claude failures (timeout, non-zero exit). A clean Claude
  # exit with no commit means Claude declined the fix — don't retry.
  for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do
    echo "=== ${target}: attempt ${attempt}/${MAX_ATTEMPTS} ==="
    git -C "${SRC_ROOT}" reset --hard "${base_ref}"
    run_claude "${prompt}" "${work_dir}/claude-${attempt}.log" || continue

    if [[ "$(git -C "${SRC_ROOT}" rev-parse HEAD)" == "${base_ref}" ]]; then
      echo "::warning::${target}: Claude declined the fix; not retrying."
      return
    fi
    open_pr "${target}" "${branch_name}"
    return
  done

  echo "::warning::${target}: ${MAX_ATTEMPTS} transient Claude failures; giving up."
}


# Download the (integration, version) targets emitted by failed omnibus jobs
# and emit a deduped JSON array. Output: ["mariadb|", "nmap|", "python|3.13"]
discover_targets() {
  echo "Downloading autofix targets from run ${RUN_ID}..." >&2

  local targets_dir="${WORK_ROOT}/targets"
  local download_log="${WORK_ROOT}/gh-download.log"
  rm -rf "${targets_dir}"
  mkdir -p "${targets_dir}"

  # gh run download exits non-zero both when no artifacts match (legitimate:
  # the run had no failures) and on real errors (auth, expired run, API down).
  # Differentiate by inspecting stderr so genuine failures aren't silenced.
  if ! gh run download "${RUN_ID}" --repo "${REPO}" \
         --pattern 'autofix-target-*' --dir "${targets_dir}" 2>"${download_log}"; then
    if grep -q "no artifacts found" "${download_log}"; then
      echo "No autofix-target artifacts in run ${RUN_ID} (run had no failures)." >&2
    else
      echo "::error::gh run download failed: $(cat "${download_log}")" >&2
      return 1
    fi
  fi

  local targets_file="${WORK_ROOT}/targets.txt"
  local integration version patch_dir
  while IFS=$'\t' read -r integration version _; do
    [[ -z "${integration}" ]] && continue
    patch_dir="${INTEGRATION_DIR}/${integration}_patch"
    [[ -d "${patch_dir}" ]] || continue
    
    # If a version was reported, skip when the patch dir has version subdirs
    # but none match this version (e.g. python|main, ruby|master, openvpn|master).
    if [[ -n "${version}" && ! -d "${patch_dir}/${version}" ]]; then
      local has_subdirs=false
      for subdir in "${patch_dir}"/*/; do
        [[ -d "${subdir}" ]] && has_subdirs=true && break
      done
      [[ "${has_subdirs}" = "true" ]] && continue
    fi
    echo "${integration}|${version}"
  done < <(find "${targets_dir}" -type f -name autofix-target.txt -exec cat {} +) \
    | sort -u > "${targets_file}"

  # Emit JSON array (empty array if no targets).
  jq -R -s -c 'split("\n") | map(select(length > 0))' "${targets_file}"
}


mode="$1"
shift

case "${mode}" in
  discover)
    setup "$1"
    discover_targets
    ;;
  fix)
    integration="$1"
    version="$2"
    setup "$3"
    base_ref=$(git -C "${SRC_ROOT}" rev-parse HEAD)
    fix_target "${integration}" "${version}" "${base_ref}"
    ;;
esac
