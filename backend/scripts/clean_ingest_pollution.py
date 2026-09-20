"""Remove the stale EXT-TEST-0099 artifact left by the old unisolated test.

Dry run by default: it prints exactly what it would touch. Pass --apply to do
it. Both the database and the ingest folder are copied to a throwaway backup
directory first, and the copies are verified before anything is removed, so
"restore" is a real option rather than a hope.

    python scripts/clean_ingest_pollution.py                 # show the plan
    python scripts/clean_ingest_pollution.py --apply         # do it

Context: `scripts/test_ingest_api.py` used to write to the real database and the
real ingest directory because it was collected by a bare `pytest` run from
`backend/`. That file now lives in `tests/` under `conftest.py` isolation, so the
row is a leftover rather than a live hazard. It does not affect scoring (the
evaluator reads the corpus, not the ingested rows), but the Review Desk lists it.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import BACKEND_ROOT, get_settings  # noqa: E402

TARGET = "EXT-TEST-0099"


def _rows_referencing(db_path: Path, target: str) -> dict[str, int]:
    """Count the rows the target id owns, table by table.

    A report is referenced from `issues` and `resolutions` by primary key, not
    by email id, so those are counted through `reports.id` as well: deleting the
    report and leaving its issues behind would orphan them rather than clean up.
    """
    conn = sqlite3.connect(db_path)
    tables = [r[0] for r in conn.execute(
        "select name from sqlite_master where type='table'")]
    report_ids = [r[0] for r in conn.execute(
        "select id from reports where email_id = ?", (target,))] \
        if "reports" in tables else []

    found: dict[str, int] = {}
    for table in tables:
        if table.startswith("sqlite_"):
            continue
        cols = [r[1] for r in conn.execute(f"pragma table_info({table})")]
        clauses, params = [], []
        if "email_id" in cols:
            clauses.append("email_id = ?")
            params.append(target)
        if "report_id" in cols and report_ids:
            clauses.append(f"report_id in ({','.join('?' * len(report_ids))})")
            params.extend(report_ids)
        if not clauses:
            continue
        count = conn.execute(
            f"select count(*) from {table} where {' or '.join(clauses)}",
            params).fetchone()[0]
        if count:
            found[table] = count
    conn.close()
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; without it nothing is modified")
    ap.add_argument("--target", default=TARGET)
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    db_path = BACKEND_ROOT / "sdoc.db"
    ingest_root = Path(get_settings().ingest_dir)
    target_dir = ingest_root / args.target

    if not db_path.is_file():
        print(f"no database at {db_path}")
        return 1

    owned = _rows_referencing(db_path, args.target)
    print(f"database        : {db_path}")
    print(f"ingest root     : {ingest_root}")
    print(f"rows owned by {args.target}: {owned or 'none'}")
    print(f"folder on disk  : {target_dir.is_dir()}"
          + (f" ({len(list(target_dir.iterdir()))} entries)"
             if target_dir.is_dir() else ""))

    if not owned and not target_dir.is_dir():
        print("\nnothing to clean")
        return 0

    if not args.apply:
        print("\ndry run. Re-run with --apply to remove the rows and the folder.")
        print("A backup of both is written next to the backend first.")
        return 0

    backup = BACKEND_ROOT / f"cleanup-backup-{stamp}"
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(db_path, backup / "sdoc.db")
    if target_dir.is_dir():
        shutil.move(str(target_dir), str(backup / args.target))
        print(f"moved  {target_dir}  ->  {backup / args.target}")
    print(f"backed up {db_path.name} -> {backup / 'sdoc.db'}")

    # Verify the backup opens and holds the same rows before deleting anything.
    check = sqlite3.connect(backup / "sdoc.db")
    backed_up = check.execute("select count(*) from emails").fetchone()[0]
    check.close()
    live = sqlite3.connect(db_path).execute(
        "select count(*) from emails").fetchone()[0]
    if backed_up != live:
        print(f"backup is inconsistent ({backed_up} vs {live} rows); aborting")
        return 1
    print(f"backup verified: {backed_up} email rows")

    conn = sqlite3.connect(db_path)
    for table in sorted(owned, key=lambda t: t != "emails"):
        cols = [r[1] for r in conn.execute(f"pragma table_info({table})")]
        if "email_id" in cols:
            cur = conn.execute(f"delete from {table} where email_id = ?",
                               (args.target,))
        else:
            cur = conn.execute(
                f"delete from {table} where report_id in "
                f"(select id from reports where email_id = ?)",
                (args.target,))
        print(f"deleted {cur.rowcount} row(s) from {table}")
    conn.commit()

    remaining = conn.execute(
        "select email_id from emails where email_id not like 'email_%'").fetchall()
    emails = conn.execute("select count(*) from emails").fetchone()[0]
    reports = conn.execute("select count(*) from reports").fetchone()[0]
    conn.close()

    print(f"\nemails {emails} / reports {reports}")
    print(f"non-corpus rows left: {remaining or 'none'}")
    print(f"backup kept at {backup}")
    assert not remaining, "non-corpus rows survived the cleanup"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
