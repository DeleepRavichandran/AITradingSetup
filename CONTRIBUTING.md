# Contributing and repository rules

## Branches and commits

- `main` is always releasable: tests pass, the dashboard builds.
- Work on a short-lived branch (`feat/…`, `fix/…`, `docs/…`); merge by pull request once CI is green.
- One logical change per commit. Messages follow Conventional Commits:
  `type(scope): imperative summary` with a body that says **why**.
  Types: `feat`, `fix`, `test`, `docs`, `refactor`, `chore`, `perf`. Scopes: `data`,
  `backtester`, `research`, `execution`, `api`, `web`, `infra`.
- Never rewrite history on a shared branch. Never force-push `main`.

## What must never be committed

`.gitignore` already blocks these — do not work around it:

- **Secrets**: `.env`, API keys, broker tokens, `HUMAN_APPROVAL_SECRET`, approval files.
- **Market data**: anything under `datasets/`. Licensed data must not be redistributed, and even
  the synthetic fixture is regenerated, not stored.
- **Generated output**: `experiments/` (registries, run artefacts, generated strategy code).

If a secret is committed by accident: rotate it first, then remove it. Removing it from history
does not un-leak it.

## Before you open a pull request

```bash
pytest -q                                   # all suites
ruff check services packages agents scripts --select E9,F63,F7,F82,F401,F841
(cd apps/web && npm run typecheck && npm run build)
```

## Rules specific to this codebase

1. **Numerical changes are deliberate.** `tests/regression/test_golden.py` pins exact result
   hashes. If your change alters them, say why in the commit body and re-pin in the same commit.
2. **Money is integer paise.** No floats in the ledger. New cash flows go through
   `Account._post` so `reconcile()` keeps holding.
3. **No look-ahead.** Strategies read data only through `ctx`. Anything that touches the
   engine loop needs a test in `tests/unit/test_engine.py`.
4. **Charges and market-structure dates are data**, in `packages/instruments/rules/*.json`, with a
   `source` and a `verified` flag. Do not mark an entry verified unless you checked the primary
   source (the circular or the Act), and cite it.
5. **Status honesty.** If something is not built or not tested against the real service, label it
   NOT IMPLEMENTED / UNVERIFIED / BLOCKED in `FEATURES` (`services/api/main.py`) and in
   `docs/IMPLEMENTATION_STATUS.md`. Do not add demo data to the UI.
6. **The live gate is not to be weakened.** Changes under `services/execution/` need a second
   human reviewer and must keep every test in `tests/integration/test_paper_and_execution.py`.
7. Keep the documents in `docs/` current in the same pull request as the change they describe.
