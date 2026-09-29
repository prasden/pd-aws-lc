Repair the failing integration below. The system prompt's rules apply to every step.

- Integration: `{name}`
- Version or branch: `{version}`
- Sandbox folder (your working directory): `{sandbox_dir}`
- Failed CI job logs: `{sandbox_dir}/logs/`
- Downstream repositories at the failing commit: {repos}
- Runner script (writable): `{runner}`
- Patch directories (writable): {patch_dirs}

Start by reading the logs. When your changes are done, you MUST call run_integration.
