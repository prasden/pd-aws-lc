You are auto-fixing a broken integration test patch in AWS-LC.

Integration: INTEGRATION_PLACEHOLDER
Version/branch: VERSION_PLACEHOLDER
Runner script: RUNNER_SCRIPT_PLACEHOLDER
Patch directory: PATCH_DIR_PLACEHOLDER
Failing run: FAILING_RUN_PLACEHOLDER
Logs: LOGS_DIR_PLACEHOLDER
Repo root: SRC_ROOT_PLACEHOLDER (on branch BRANCH_NAME_PLACEHOLDER, cut from main)
RETRY_CONTEXT_PLACEHOLDER

Steps:
1. Read the runner script — find the downstream repo URL, branch/tag, and how patches are applied.
2. Search the downstream repo's recent commits and issues on GitHub for context on what changed. Also check FAILING_RUN_PLACEHOLDER for error details.
3. Clone downstream into WORK_ROOT_PLACEHOLDER/INTEGRATION_PLACEHOLDER/src. If a specific version/branch is listed above, clone that branch.
4. Run `patch --dry-run -p1` with existing patches to identify failing hunks. Only fix patches for the failing version — do not modify patches for other versions.
5. Read the failing source context in the cloned repo to understand what changed upstream.
6. Author corrected patches, replacing broken ones. If a hunk is redundant (upstream already does what the patch did), delete it.
7. Validate: run `patch --dry-run -p1` against a fresh clone of the downstream repo at the correct branch. Every patch must apply with zero fuzz and zero rejects. If validation fails, go back to step 6 and fix the patches until they pass.
8. Stage and commit with a descriptive message. Title: `autofix(INTEGRATION_PLACEHOLDER): repair patch (run RUN_ID_PLACEHOLDER)`. Body (after blank line): 3 sentences — why the patch was failing, what changed upstream, and how you fixed it.

Do NOT push or open a PR. If you cannot fix it, print 'AUTOFIX_FAILED: INTEGRATION_PLACEHOLDER' and exit.
