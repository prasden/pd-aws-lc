#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0 OR ISC

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"

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

  if [ -z "${GH_TOKEN:-}" ]; then
    echo "GH_TOKEN is not set; cannot push branches or open PRs."
    exit 1
  fi
  git -C "${SRC_ROOT}" config user.email "aws-lc-ci@amazon.com"
  git -C "${SRC_ROOT}" config user.name "AWS-LC CI Autofix"
}


# Fetch last 200 lines of each failed job's log for Claude context.
fetch_logs() {
  mkdir -p "$2"
  for job_id in $(gh api "/repos/${SOURCE_REPO}/actions/runs/${RUN_ID}/jobs" \
    --paginate \
    --jq ".jobs[]
      | select(.conclusion == \"failure\" and (.name | startswith(\"${1//_/-}\")))
      | .id")
  do
    gh api "/repos/${SOURCE_REPO}/actions/jobs/${job_id}/logs" \
      | tail -n 200 > "$2/${job_id}.log" || true
  done
}

build_prompt() {
  sed -e "s|INTEGRATION_PLACEHOLDER|$1|g" \
      -e "s|VERSION_PLACEHOLDER|${2:-all}|g" \
      -e "s|PATCH_DIR_PLACEHOLDER|$3|g" \
      -e "s|RUNNER_SCRIPT_PLACEHOLDER|$4|g" \
      -e "s|LOGS_DIR_PLACEHOLDER|$5|g" \
      -e "s|BRANCH_NAME_PLACEHOLDER|$6|g" \
      -e "s|RETRY_CONTEXT_FILE_PLACEHOLDER|${7:-/dev/null}|g" \
      -e "s|FAILING_RUN_PLACEHOLDER|https://github.com/${SOURCE_REPO}/actions/runs/${RUN_ID}|g" \
      -e "s|SRC_ROOT_PLACEHOLDER|${SRC_ROOT}|g" \
      -e "s|WORK_ROOT_PLACEHOLDER|${WORK_ROOT}|g" \
      -e "s|RUN_ID_PLACEHOLDER|${RUN_ID}|g" \
      "${SCRIPT_DIR}/prompt.md"
}

# Hardcoded downstream repo URLs per integration (source of truth for validation).
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
    ntp)           echo "" ;;  # ntp uses tarball, not git
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

# Validate patches apply cleanly against a fresh downstream clone.
# $1 = integration name, $2 = patch directory, $3 = version/branch (optional)
validate_patches() {
  local clone_url
  clone_url=$(clone_url_for "$1")
  [ -z "${clone_url}" ] && return 0  # no validation if URL unknown

  local vdir="${WORK_ROOT}/validate-tmp"
  rm -rf "${vdir}"

  local patch_src="$2"
  if [ -n "$3" ] && [ -d "$2/$3" ]; then
    patch_src="$2/$3"
    git clone --depth 1 --branch "$3" "${clone_url}" "${vdir}" || return 1
  else
    git clone --depth 1 "${clone_url}" "${vdir}" || return 1
  fi

  local fails=0
  for f in $(find -L "${patch_src}" -type f -name '*.patch'); do
    if ! patch --dry-run -p1 -d "${vdir}" < "${f}" >/dev/null 2>&1; then
      echo "FAIL: $(basename "${f}")"
      fails=$((fails + 1))
    fi
  done

  rm -rf "${vdir}"
  [ "${fails}" -eq 0 ]
}

run_claude() {
  set +e
  timeout "${CLAUDE_TIMEOUT}" claude -p "$1" \
    --allowedTools "Read,Glob,Grep,Bash,Edit,Write,Agent,WebFetch" \
    --verbose > "$2"
  local rc=$?
  set -e
  if [ "${rc}" -eq 124 ]; then
    echo "::warning::Claude timed out after ${CLAUDE_TIMEOUT}s."
  elif [ "${rc}" -ne 0 ]; then
    echo "::warning::Claude exited with code ${rc}."
  fi

  return "${rc}"
}

open_pr() {

  # Skip if the remote branch already has the same changes, so no new PRs or incessant commits are created. 
  if git -C "${SRC_ROOT}" fetch \
    "https://x-access-token:${GH_TOKEN}@github.com/${TARGET_REPO}.git" \
    "$2" 2>/dev/null; then
    if git -C "${SRC_ROOT}" diff --quiet FETCH_HEAD HEAD -- ; then
      echo "No new changes for $1; skipping push."
      return
    fi
  fi

  git -C "${SRC_ROOT}" push --force \
    "https://x-access-token:${GH_TOKEN}@github.com/${TARGET_REPO}.git" \
    "$2"
  gh pr create --draft --repo "${TARGET_REPO}" \
    --head "$2" \
    --title "autofix($1): repair patch" \
    --body "$(git -C "${SRC_ROOT}" log -1 --format=%b)
    ---
    This PR was drafted automatically by the nightly autofix workflow using Claude Code. A maintainer should review and run CI before merging.
    Triggered by: https://github.com/${SOURCE_REPO}/actions/runs/${RUN_ID}" \
    || true
}

# --- Main ---------------------------------------------------------------------

setup "$1"

# 1. Discover failed jobs.
echo "Fetching failed jobs from run ${RUN_ID}..."
FAILED_JOBS_FILE="${WORK_ROOT}/failed-jobs.txt"
gh api "/repos/${SOURCE_REPO}/actions/runs/${RUN_ID}/jobs" --paginate \
  --jq '.jobs[] | select(.conclusion == "failure" and .name != "report-failures" and .name != "autofix") | .name' \
  > "${FAILED_JOBS_FILE}"

if [ ! -s "${FAILED_JOBS_FILE}" ]; then
  echo "No failed jobs; nothing to autofix."
  exit 0
fi
cat "${FAILED_JOBS_FILE}"


# 2. Map failed job names → integration + version.
# Deduplicates so python-3.13-fips-... and python-3.13-crt-... become one fix.
# Skips jobs with no patch directory or no patchable version.
TARGETS_FILE="${WORK_ROOT}/targets.txt"
: > "${TARGETS_FILE}"

while IFS= read -r job; do
  [ -z "${job}" ] && continue
  if   [[ "${job}" == tpm2-tools* ]];       then integration="tpm2_tools"; version=""
  elif [[ "${job}" == tpm2-tss* ]];         then integration="tpm2_tss"; version=""
  elif [[ "${job}" == postgresql* ]];       then integration="postgres"; version=""
  elif [[ "${job}" == python-main* ]];      then continue  # no patches for python main
  elif [[ "${job}" == python-* ]];          then integration="python"; version=$(echo "${job}" | cut -d- -f2)
  elif [[ "${job}" == ruby-master* ]];      then integration="ruby"; version=""  # uses ruby_patch_common only
  elif [[ "${job}" == ruby-* ]];            then integration="ruby"; version=$(echo "${job}" | cut -d- -f2)
  elif [[ "${job}" == openldap-v2.5* ]];    then integration="openldap"; version="OPENLDAP_REL_ENG_2_5"
  elif [[ "${job}" == openldap-* ]];        then integration="openldap"; version=$(echo "${job}" | cut -d- -f2)
  elif [[ "${job}" == openvpn-master* ]];   then continue  # no patches for openvpn master
  elif [[ "${job}" == openvpn-* ]];         then integration="openvpn"; version=""
  else integration="${job%%-*}"; version=""
  fi
  [ -d "${INTEGRATION_DIR}/${integration}_patch" ] || continue
  echo "${integration}|${version}" >> "${TARGETS_FILE}"
done < "${FAILED_JOBS_FILE}"

sort -u -o "${TARGETS_FILE}" "${TARGETS_FILE}"

if [ ! -s "${TARGETS_FILE}" ]; then
  echo "No failed jobs map to a known patch directory."
  exit 0
fi
echo "Targets to fix:"
cat "${TARGETS_FILE}"

# Capture base ref once — every target branches off the same starting commit.
base_ref=$(git -C "${SRC_ROOT}" rev-parse HEAD)

while IFS='|' read -r integration version; do
  [ -z "${integration}" ] && continue
  patch_dir="${INTEGRATION_DIR}/${integration}_patch"
  runner_script="${INTEGRATION_DIR}/run_${integration}_integration.sh"
  [ -d "${patch_dir}" ] && [ -f "${runner_script}" ] || continue

  target="${integration}${version:+-${version}}"
  branch_name="autofix/${target}"
  work_dir="${WORK_ROOT}/${target}"
  retry_file="${work_dir}/retry-context.txt"
  mkdir -p "${work_dir}"
  rm -f "${retry_file}"

  git -C "${SRC_ROOT}" branch -D "${branch_name}" 2>/dev/null || true
  git -C "${SRC_ROOT}" checkout -B "${branch_name}" "${base_ref}"
  fetch_logs "${integration}" "${work_dir}/logs"

  fixed=false
  for attempt in $(seq 1 "${MAX_ATTEMPTS}"); do
    echo "=== ${target}: attempt ${attempt}/${MAX_ATTEMPTS} ==="
    prompt=$(build_prompt "${integration}" "${version}" "${patch_dir}" "${runner_script}" \
      "${work_dir}/logs" "${branch_name}" "${retry_file}")
    if ! run_claude "${prompt}" "${work_dir}/claude-${attempt}.log"; then
      echo "::warning::Claude failed on attempt ${attempt}; retrying..."
      git -C "${SRC_ROOT}" reset --hard "${base_ref}"
      rm -f "${retry_file}"
      continue
    fi
    # Claude succeeded — validate the patches ourselves.
    if validation_output=$(validate_patches "${integration}" "${patch_dir}" "${version}" 2>&1); then
      fixed=true
      break
    fi
    echo "::warning::Patch validation failed on attempt ${attempt}; retrying..."
    echo "${validation_output}"
    {
      echo "IMPORTANT: Your previous patches FAILED independent validation:"
      echo "${validation_output}"
      echo ""
      echo "Fix the patches so they apply cleanly with zero fuzz and zero rejects."
    } > "${retry_file}"
    git -C "${SRC_ROOT}" reset --hard "${base_ref}"
  done

  if [ "${fixed}" = "true" ]; then
    open_pr "${target}" "${branch_name}"
  else
    echo "::warning::Could not fix ${target} after ${MAX_ATTEMPTS} attempts."
  fi
done < "${TARGETS_FILE}"

echo "Autofix complete."
exit 0
