# Offline maintenance

Optional Python 3 maintenance helpers for legacy-to-v3 migration, independent verification and central handoff. These sources are separate from the five-tool Node runtime. Only synthetic test fixtures are included; no databases, real project identifiers, backups, credentials or deployment evidence are shipped.

## Usage

From this directory:

```sh
python3 scripts/fact_maintenance.py --help
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q plugin/tests
```

Migration creates an offline candidate and preserves source rows and table-scoped legacy identities. Handoff prepare/verify/register does not publish work files or authorize adoption. Back up consistently, verify candidates independently, obtain authorization before publishing/adopting, and preserve archived status.

Parentless-task verification filters parent relations by source table (`tasks`), because goal/task numeric IDs may coincide. Regression tests cover this collision, correct links, source preservation and tamper rejection.

The central catalog is derived. Missing paths are not automatically deleted: they may reflect unavailable mounts. `reindex` rebuilds the catalog from existing registrations; it does not prune registrations. Do not treat a missing path alone as authorization for deletion.
