#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0 OR ISC
#
# Drives the autofix loop for failed integration tests. For each failed job:
#   1. Asks Claude (via Bedrock) to repair the patch
#   2. Validates the result against a fresh downstream clone
#   3. Pushes the fix to a branch and opens a draft PR

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
  local retry_file="${7:-/dev/null}"

  sed -e "s|INTEGRATION_PLACEHOLDER|${integration}|g" \
      -e "s|VERSION_PLACEHOLDER|${version:-all}|g" \
      -e "s|PATCH_DIR_PLACEHOLDER|${patch_dir}|g" \
      -e "s|RUNNER_SCRIPT_PLACEHOLDER|${runner_script}|g" \
      -e "s|LOGS_DIR_PLACEHOLDER|${logs_dir}|g" \
      -e "s|BRANCH_NAME_PLACEHOLDER|${branch_name}|g" \
      -e "s|RETRY_CONTEXT_FILE_PLACEHOLDER|${retry_file}|g" \
      -e "s|FAILING_RUN_PLACEHOLDER|https://github.com/${SOURCE_REPO}/actions/runs/${RUN_ID}|g" \
      -e "s|SRC_ROOT_PLACEHOLDER|${SRC_ROOT}|g" \
      -e "s|WORK_ROOT_PLACEHOLDER|${WORK_ROOT}|g" \
      -e "s|RUN_ID_PLACEHOLDER|${RUN_ID}|g" \
      "${SCRIPT_DIR}/prompt.md"
}

# Source-of-truth map from integration name → downstream git URL.
# Used to independently validate Claude's patches against fresh upstream code.
clone_url_for() {
  case "$1" in
    bind9)         echo "https://gitlab.isc.org/isc-projects/bind9.git" ;;
    libgit2)       echo "https://github.com/libgit2/libgit2.git" ;;
    librdkafka)    echo "https://github.com/confluentinc/librdkafka.git" ;;
    librelp)       echo "https://github.com/rsyslog/librelp.git" ;;
    libwebsockets) echo "https://github.com/warmcat/libwebsockets.git" ;;
    mariadb)       echo "https://github.com/MariaDB/server.git" ;;
    mysql)         echo "https://github.com/mysql/mysql-server.git" ;;
    nmap)          echo "https://github.com/nmap/nmap.git" ;;
    ntp)           echo "" ;;  # ntp uses a tarball, no validation
    openldap)      echo "https://github.com/openldap/openldap.git" ;;
    openvpn)       echo "https://github.com/OpenVPN/openvpn.git" ;;
    postgres)      echo "https://github.com/postgres/postgres.git" ;;
    pyopenssl)     echo "https://github.com/pyca/pyopenssl.git" ;;
    python)        echo "https://github.com/python/cpython.git" ;;
    ruby)          echo "https://github.com/ruby/ruby.git" ;;
    sslproxy)      echo "https://github.com/sonertari/SSLproxy.git" ;;
    tcpdump)       echo "https://github.com/the-tcpdump-group/tcpdump.git" ;;
    tpm2_tools)    echo "https://github.com/tpm2-software/tpm2-tools.git" ;;
    tpm2_tss)      echo "https://github.com/tpm2-software/tpm2-tss.git" ;;
    trousers)      echo "https://git.code.sf.net/p/trousers/trousers" ;;
  esac
}

# Run `patch --dry-run` against a fresh downstream clone. Returns 0 if all
# patches apply cleanly, non-zero otherwise (with FAIL: lines on stdout).
validate_patches() {
  local integration="$1"
  local patch_dir="$2"
  local version="$3"

  local clone_url
  clone_url=$(clone_url_for "${integration}")
  # If no URL is defined (e.g. ntp uses a tarball), skip validation and treat
  # as success so the PR still gets opened.
  [[ -z "${clone_url}" ]] && return 0

  # Some patch dirs are laid out by branch (e.g. python_patch/3.13)
  # We need to validate only the failing branch.
  local patch_src="${patch_dir}"
  local branch_arg=()
  if [[ -n "${version}" && -d "${patch_dir}/${version}" ]]; then
    patch_src="${patch_dir}/${version}"
    branch_arg=(--branch "${version}")
  fi

  local validation_directory="${WORK_ROOT}/validate-tmp"
  rm -rf "${validation_directory}"
  git clone --depth 1 "${branch_arg[@]}" "${clone_url}" "${validation_directory}" || return 1

  local fails=0
  local patch_file
  while IFS= read -r patch_file; do
    if ! patch --dry-run -p1 -d "${validation_directory}" < "${patch_file}" >/dev/null 2>&1; then
      echo "FAIL: $(basename "${patch_file}")"
      fails=$((fails + 1))
    fi
  done < <(find -L "${patch_src}" -type f -name '*.patch')

  rm -rf "${validation_directory}"
  [[ "${fails}" -eq 0 ]]
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

  gh pr create --draft --repo "${TARGET_REPO}" \
    --head "${branch_name}" \
    --title "autofix(${target}): repair patch" \
    --body  "$(git -C "${SRC_ROOT}" log -1 --format=%b)
    ---
    This PR was drafted automatically by the nightly autofix workflow using Claude Code. A maintainer should review and run CI before merging.
    Triggered by: https://github.com/${SOURCE_REPO}/actions/runs/${RUN_ID}" \
    || true  # PR may already exist; that's fine.
}

# Map a failed job name to its (integration, version) tuple.
# Echoes "integration|version" on stdout, or nothing if the job has no patches.
classify_job() {
  local job="$1"
  local integration version

  case "${job}" in
    tpm2-tools*)        integration="tpm2_tools"; version="" ;;
    tpm2-tss*)          integration="tpm2_tss";   version="" ;;
    postgresql*)        integration="postgres";   version="" ;;
    python-main*)       return ;;  # no patches for python main
    python-*)           integration="python";     version=$(cut -d- -f2 <<< "${job}") ;;
    ruby-master*)       integration="ruby";       version="" ;;  # uses ruby_patch_common only
    ruby-*)             integration="ruby";       version=$(cut -d- -f2 <<< "${job}") ;;
    openldap-v2.5*)     integration="openldap";   version="OPENLDAP_REL_ENG_2_5" ;;
    openldap-*)         integration="openldap";   version=$(cut -d- -f2 <<< "${job}") ;;
    openvpn-master*)    return ;;  # no patches for openvpn master
    openvpn-*)          integration="openvpn";    version="" ;;
    *)                  integration="${job%%-*}"; version="" ;;
  esac

  [[ -d "${INTEGRATION_DIR}/${integration}_patch" ]] || return 0
  echo "${integration}|${version}"
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
  local retry_file="${work_dir}/retry-context.txt"

  mkdir -p "${work_dir}"
  rm -f "${retry_file}"

  # Start from a clean branch off the base commit.
  git -C "${SRC_ROOT}" branch -D "${branch_name}" 2>/dev/null || true
  git -C "${SRC_ROOT}" checkout -B "${branch_name}" "${base_ref}"

  fetch_logs "${integration}" "${work_dir}/logs"

  local attempt validation_output
  for attempt in $(seq 1 "${MAX_ATTEMPTS}"); do
    echo "=== ${target}: attempt ${attempt}/${MAX_ATTEMPTS} ==="

    local prompt
    prompt=$(build_prompt "${integration}" "${version}" "${patch_dir}" \
                          "${runner_script}" "${work_dir}/logs" \
                          "${branch_name}" "${retry_file}")

    if ! run_claude "${prompt}" "${work_dir}/claude-${attempt}.log"; then
      echo "::warning::Claude failed on attempt ${attempt}; retrying..."
      git -C "${SRC_ROOT}" reset --hard "${base_ref}"
      rm -f "${retry_file}"
      continue
    fi

    if validation_output=$(validate_patches "${integration}" "${patch_dir}" "${version}" 2>&1); then
      open_pr "${target}" "${branch_name}"
      return
    fi

    # Validation failed: feed the error back to Claude on the next attempt.
    echo "::warning::Patch validation failed on attempt ${attempt}; retrying..."
    echo "${validation_output}"
    {
      echo "IMPORTANT: Your previous patches FAILED independent validation:"
      echo "${validation_output}"
      echo
      echo "Fix the patches so they apply cleanly with zero fuzz and zero rejects."
    } > "${retry_file}"
    git -C "${SRC_ROOT}" reset --hard "${base_ref}"
  done

  echo "::warning::Could not fix ${target} after ${MAX_ATTEMPTS} attempts."
}


# Discover failed jobs and emit a deduped JSON array of targets.
# Output: ["mariadb|", "nmap|", "python|3.13"]
discover_targets() {
  echo "Fetching failed jobs from run ${RUN_ID}..." >&2

  local failed_jobs_file="${WORK_ROOT}/failed-jobs.txt"
  gh api "/repos/${SOURCE_REPO}/actions/runs/${RUN_ID}/jobs" --paginate \
    --jq '.jobs[]
          | select(.conclusion == "failure" and .name != "report-failures" and .name != "autofix")
          | .name' \
    > "${failed_jobs_file}"

  local targets_file="${WORK_ROOT}/targets.txt"
  : > "${targets_file}"

  local job
  while IFS= read -r job; do
    [[ -z "${job}" ]] && continue
    classify_job "${job}" >> "${targets_file}" || true
  done < "${failed_jobs_file}"

  sort -u -o "${targets_file}" "${targets_file}"

  # Emit JSON array (empty array if no targets).
  jq -R -s -c 'split("\n") | map(select(length > 0))' "${targets_file}"
}


# ---------- Main --------------------------------------------------------------

mode="${1:?Usage: $0 {discover <run_id> | fix <integration> <version> <run_id>}}"
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
    echo "Usage: $0 {discover <run_id> | fix <integration> <version> <run_id>}" >&2
    exit 1
    ;;
esac
