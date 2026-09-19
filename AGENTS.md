# Repository guide for coding agents

## Start here

Read README.md, docs/prerequisites.md, and docs/architecture.md before changing behavior. This is a single-process FastAPI + SQLite bridge between WeChat iLink and Alibaba Bailian Managed Agent. Static JavaScript requires no frontend build. Python 3.11+ is required.

## Local workflow

- Install development dependencies: `python -m pip install -r requirements-dev.txt`.
- Run tests: `python -m pytest -q` (temporary databases, fake credentials; no cloud account required).
- Frontend syntax: `node --check static/app.js`.
- Linux launcher syntax: `bash -n start.sh`.
- Build the cloud Agent Skill: `python scripts/package_skill.py`.
- Resource provisioning: `scripts/bootstrap_ma.py` defaults to an offline preview; `--apply` creates cloud resources. See docs/bootstrap.md. Never run --apply as part of tests.
- Check files before publishing: `python scripts/check_repository.py --staged` (or omit --staged for tracked files).
- Do not start another worker against an existing live database. Use an isolated database for previews.

## Design constraints

1. One process / one Uvicorn worker. Every process starts WeChat polling and schedule scanning.
2. Keep durable state in SQLite; extend migrations in app/store.py without resetting data.
3. A network timeout does not establish that cloud creation, message submission or WeChat delivery failed. Preserve uncertain states and reconcile instead of blindly repeating side effects.
4. Foreground chat, scheduled Session and SSE buffers must remain isolated. Scheduled execution never replaces bots.session_id.
5. Keep per-Bot memory, identity, API authorization and user matching intact. Tokens do not belong in shared Agent configurations.
6. Charges use integer micro-yuan, Decimal rounding, frozen rates, incremental usage, unique job IDs and remote tool call IDs. Read docs/billing.md before changing formulas.
7. Only assistant message text and registered artifacts are delivered to WeChat; do not forward tool/reasoning events.
8. Natural-language account/schedule requests use the bundled Skill. Exact /usage and /clear are backend commands.

## Files and documentation

Follow the existing Python/JS style in the touched file; avoid mass formatting unrelated code. Add focused regression tests for state, auth or billing changes. Keep README, API examples and Skill command documentation consistent with implementation. If app/ma.py prompt constants change, synchronize docs/agent-system-prompt.txt.

## Secrets and external state

Never commit .env, data/, local-access.txt, logs, database files, exports or generated ZIPs. Do not print credentials while debugging. tests/conftest.py supplies fake configuration before app imports. Do not run real cloud or WeChat side effects, deploy, upload a Skill, or change a remote Agent unless the user explicitly requests those actions. There is no repository-specific external account to use.

Do not describe mocked integration tests as real mobile/cloud end-to-end verification. Document any unverified deployment step honestly.
