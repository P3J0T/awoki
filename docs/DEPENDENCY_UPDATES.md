# Dependency verification and updates

`requirements.txt` declares supported Python version ranges. `requirements.lock`
constrains direct and transitive packages to the versions exercised in the Linux
Python 3.12 runtime. Both Dockerfiles install with these constraints, run `pip check`,
and record their resolved inventory in `/usr/local/share/awoki-python-resolved.txt`.
This prevents silent Python dependency upgrades on an otherwise identical build.
It is not full binary reproducibility: wheel hashes, platform differences, base-image
digests and Debian packages are not frozen by this file.

For an intentional Python update, resolve `requirements.txt` in a clean Python 3.12
Linux environment without the old constraints, run `pip check`, and regenerate the
constraints from that environment's `pip freeze`. Review the version diff, update
`.harness/runtime-dependencies.lock.json` when direct requirements/policy change,
and rerun the complete suite plus `make ai-config-test`. Never derive the lock from
an unrelated host environment or copy provider credentials into the test environment.

OpenCode still follows the existing latest-by-default policy, with an explicitly
chosen safe version available for rollback. Python constraints do not pin OpenCode
to an old release. The image records matching CLI/plugin/SDK versions as before.

The GitHub validation workflow resolves matching OpenCode plugin/SDK packages at
the current CLI version and runs `.harness/validate_opencode_plugin.py --require-sdk`.
It checks the real SDK types and exercises session hooks. The same strict check can
be run locally with `AWOKI_PLUGIN_NODE_MODULES` pointing to an isolated dependency
directory containing matching plugin/SDK packages, TypeScript and Node types.
The currently tested compiler tooling is TypeScript 7.0.2 with `@types/node` 22.20.2.
The portable no-SDK smoke check does not certify SDK compatibility or a complete
OpenCode conversation lifecycle. CI also exercises provider HTTP contracts with
synthetic keys on loopback; no live inference service is required.
