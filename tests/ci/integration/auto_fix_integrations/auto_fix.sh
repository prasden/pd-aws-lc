#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0 OR ISC
#
# Drives the autofix loop for failed integration tests. Two modes:
#   discover  - read the (integration, version) targets emitted by failed
#               omnibus jobs and print them as a JSON array
#   fix       - for one target, ask Claude (via Bedrock) to repair the patch
#               and, if it commits a fix, open a draft PR. Claude validates the
#               patch with `patch --dry-run`; the draft PR's CI is the backstop.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"


# ---------- Setup -------------------------------------------------------------

setup() {
  RUN_ID="${1:?Usage: $0 <integration_omnibus_run_id>}"
  SOURCE_REPO="${GITHUB_REPO_OWNER}/${GITHUB_REPO_NAME}"
  TARGET_REPO="${AUTOFIX_TARGET_REPO:-$(git -C "${SRC_ROOT}" remote get-url origin \
    | sed -E 's#(https://github.com/|git@github.com:)([^/]+/[^/.]+)(\.git)?#\2#')}"
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


# ---------- Helpers -----------------------------------------------------------

# Fetch the last 200 lines of each failed job's log into the given directory.
# Args: $1 = integration name, $2 = output directory
fetch_logs() {
  local integration="$1"
  local logs_dir="$2"
  local prefix="${integration//_/-}"

  mkdir -p "${logs_dir}"

  local job_id
  for job_id in $(gh api "/repos/${SOURCE_REPO}/actions/runs/${RUN_ID}/jobs" \
                    --paginate \
                    --jq ".jobs[]
                          | select(.conclusion == \"failure\" and (.name | startswith(\"${prefix}\")))
                          | .id")
  do
    gh api "/repos/${SOURCE_REPO}/actions/jobs/${job_id}/logs" \
      | tail -n 200 > "${logs_dir}/${job_id}.log" || true
  done
}

# Render the prompt template with the given context.
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
      -e "s|FAILING_RUN_PLACEHOLDER|https://github.com/${SOURCE_REPO}/actions/runs/${RUN_ID}|g" \
      -e "s|SRC_ROOT_PLACEHOLDER|${SRC_ROOT}|g" \
      -e "s|WORK_ROOT_PLACEHOLDER|${WORK_ROOT}|g" \
      -e "s|RUN_ID_PLACEHOLDER|${RUN_ID}|g" \
      "${SCRIPT_DIR}/prompt.md"
}

run_claude() {
  local prompt="$1"
  local log_file="$2"

  set +e
  timeout "${CLAUDE_TIMEOUT}" claude -p "${prompt}" \
    --allowedTools "Read,Glob,Grep,Bash,Edit,Write,Agent,WebFetch" \
    --verbose > "${log_file}"
  local rc=$?
  set -e

  if [[ "${rc}" -eq 124 ]]; then
    echo "::warning::Claude timed out after ${CLAUDE_TIMEOUT}s."
  elif [[ "${rc}" -ne 0 ]]; then
    echo "::warning::Claude exited with code ${rc}."
  fi

  return "${rc}"
}

# Push the fix branch and open (or update) the draft PR.
open_pr() {
  local target="$1"
  local branch_name="$2"
  local push_url="https://x-access-token:${GH_TOKEN}@github.com/${TARGET_REPO}.git"

  # Skip if the remote branch already has the same changes (avoids re-pushing
  # identical fixes night after night).
  if git -C "${SRC_ROOT}" fetch "${push_url}" "${branch_name}" 2>/dev/null \
     && git -C "${SRC_ROOT}" diff --quiet FETCH_HEAD HEAD; then
    echo "No new changes for ${target}; skipping push."
    return
  fi

  git -C "${SRC_ROOT}" push --force "${push_url}" "${branch_name}"

  local pr_body
  pr_body="$(git -C "${SRC_ROOT}" log -1 --format=%b)

---
This PR was drafted automatically by the nightly autofix workflow using Claude Code. A maintainer should review and run CI before merging.
Triggered by: https://github.com/${SOURCE_REPO}/actions/runs/${RUN_ID}"

  gh pr create --draft --repo "${TARGET_REPO}" \
    --head "${branch_name}" \
    --title "autofix(${target}): repair patch" \
    --body "${pr_body}" \
    || true  # PR may already exist; that's fine.
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

  # Start from a clean branch off the base commit.
  git -C "${SRC_ROOT}" branch -D "${branch_name}" 2>/dev/null || true
  git -C "${SRC_ROOT}" checkout -B "${branch_name}" "${base_ref}"

  fetch_logs "${integration}" "${work_dir}/logs"

  local prompt attempt
  prompt=$(build_prompt "${integration}" "${version}" "${patch_dir}" \
                        "${runner_script}" "${work_dir}/logs" "${branch_name}")

  # Claude validates patches with `patch --dry-run` before committing (see
  # prompt.md). The draft PR's CI is the deterministic backstop. We retry only
  # on transient Claude failures (timeout, non-zero exit). If Claude finishes
  # cleanly without a commit, that's Claude declining the fix — retrying with
  # the same prompt won't change the answer.
  for attempt in $(seq 1 "${MAX_ATTEMPTS}"); do
    echo "=== ${target}: attempt ${attempt}/${MAX_ATTEMPTS} ==="
    git -C "${SRC_ROOT}" reset --hard "${base_ref}"
    if ! run_claude "${prompt}" "${work_dir}/claude-${attempt}.log"; then
      continue  # transient failure — try again
    fi
    if [[ "$(git -C "${SRC_ROOT}" rev-parse HEAD)" != "${base_ref}" ]]; then
      open_pr "${target}" "${branch_name}"
      return
    fi
    echo "::warning::${target}: Claude produced no commit; declining further attempts."
    return
  done

  echo "::warning::Could not fix ${target} after ${MAX_ATTEMPTS} transient failures."
}


# Download the (integration, version) targets emitted by failed omnibus jobs
# and emit a deduped JSON array. Output: ["mariadb|", "nmap|", "python|3.13"]
discover_targets() {
  echo "Downloading autofix targets from run ${RUN_ID}..." >&2

  local targets_dir="${WORK_ROOT}/targets"
  rm -rf "${targets_dir}"
  mkdir -p "${targets_dir}"
  gh run download "${RUN_ID}" --repo "${SOURCE_REPO}" \
    --pattern 'autofix-target-*' --dir "${targets_dir}" 2>/dev/null || true

  local targets_file="${WORK_ROOT}/targets.txt"
  local integration version patch_dir
  while IFS=$'\t' read -r integration version _; do
    [[ -z "${integration}" ]] && continue
    patch_dir="${INTEGRATION_DIR}/${integration}_patch"
    # Skip integrations with no patch directory at all.
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
  done < <(cat "${targets_dir}"/*/autofix-target.txt 2>/dev/null) \
    | sort -u > "${targets_file}"

  # Emit JSON array (empty array if no targets).
  jq -R -s -c 'split("\n") | map(select(length > 0))' "${targets_file}"
}


# ---------- Main --------------------------------------------------------------

if [[ -z "${1:-}" ]]; then
  echo "Usage: $0 discover <run_id>" >&2
  echo "       $0 fix <integration> <version> <run_id>" >&2
  exit 1
fi
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
  *)
    echo "Unknown mode: ${mode}" >&2
    echo "Usage: $0 discover <run_id>" >&2
    echo "       $0 fix <integration> <version> <run_id>" >&2
    exit 1
    ;;
esac
