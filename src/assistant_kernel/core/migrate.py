"""Ordered, run-once SQL migrations shipped inside the package.

Files in ``core/sql`` are applied in name order, each in its own transaction,
and recorded in ``assistant.schema_migrations``. An applied file must never be
edited; add a new file instead. Hashes are taken over the content with CRLF
normalized to LF, so a Windows (CRLF) and a Git (LF) checkout of the same file
match; rows recorded from CRLF bytes before that normalization still match.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import psycopg

from assistant_kernel.config import KernelSettings

SQL_DIR = Path(__file__).with_name("sql")
LEDGER_SQL = """
CREATE SCHEMA IF NOT EXISTS assistant;
CREATE TABLE IF NOT EXISTS assistant.schema_migrations (
    name text PRIMARY KEY,
    sha256 text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def connection_args(settings: KernelSettings) -> dict[str, Any]:
    return {
        "host": settings.postgres_host,
        "port": settings.postgres_port,
        "dbname": settings.postgres_database,
        "user": settings.postgres_user,
        "password": settings.postgres_password.get_secret_value(),
        "connect_timeout": 10,
    }


def digests(raw: bytes) -> tuple[str, str]:
    """Return (hash to record, legacy CRLF-bytes hash) for a migration file."""
    text = raw.replace(b"\r\n", b"\n")
    return (hashlib.sha256(text).hexdigest(),
            hashlib.sha256(text.replace(b"\n", b"\r\n")).hexdigest())


def apply(args: dict[str, Any], directory: Path = SQL_DIR, *, dry_run: bool = False) -> list[str]:
    """Apply pending migrations; return the names applied (or pending, for a dry run)."""
    with psycopg.connect(**args) as connection:
        if dry_run:
            connection.execute("SET TRANSACTION READ ONLY")
            exists = connection.execute("SELECT to_regclass('assistant.schema_migrations')").fetchone()[0]
            applied = dict(connection.execute(
                "SELECT name, sha256 FROM assistant.schema_migrations").fetchall()) if exists else {}
        else:
            connection.execute(LEDGER_SQL)
            connection.commit()
            applied = dict(connection.execute(
                "SELECT name, sha256 FROM assistant.schema_migrations").fetchall())
        names = []
        for path in sorted(directory.glob("*.sql")):
            digest, crlf_digest = digests(path.read_bytes())
            if path.name in applied:
                if applied[path.name] not in (digest, crlf_digest):
                    raise RuntimeError(f"applied migration {path.name} was edited")
                continue
            names.append(path.name)
            if dry_run:
                continue
            with connection.transaction():
                connection.execute(path.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO assistant.schema_migrations(name, sha256) VALUES (%s, %s)",
                    (path.name, digest))
        return names


def backup_tools() -> tuple[str, str]:
    tools = tuple(shutil.which(name) for name in ("pg_dump", "pg_restore"))
    if not all(tools):
        raise RuntimeError("pg_dump and pg_restore must be available before deployment")
    return tools


def backup_database(args: dict[str, Any], target: Path) -> None:
    """Keep a custom-format recovery artifact; never restore a live database automatically."""
    dump, restore = backup_tools()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Refuse overwrites and restrict permissions before pg_dump writes any data.
    with target.open("xb"):
        target.chmod(0o600)
    env = {**os.environ, "PGPASSWORD": args["password"], "PGCONNECT_TIMEOUT": "10"}
    subprocess.run([dump, "--host", str(args["host"]), "--port", str(args["port"]),
                    "--username", args["user"], "--dbname", args["dbname"], "--no-password",
                    "--format=custom", "--file", str(target)], env=env, check=True, timeout=600)
    if not target.stat().st_size:
        raise RuntimeError("database backup is empty")
    subprocess.run([restore, "--list", str(target)], check=True, timeout=60, stdout=subprocess.DEVNULL)
    # Listing verifies the archive structure, not a complete restore rehearsal.


def run() -> None:
    parser = argparse.ArgumentParser(description="Apply Kernel core SQL migrations")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check-backup-tools", action="store_true")
    mode.add_argument("--backup", type=Path)
    options = parser.parse_args()
    if options.check_backup_tools:
        backup_tools()
        print("BACKUP_TOOLS_OK")
        return
    args = connection_args(KernelSettings.from_environment())
    if options.backup:
        backup_database(args, options.backup)
        print(f"BACKUP_OK={options.backup}")
        return
    names = apply(args, dry_run=options.dry_run)
    verb = "pending" if options.dry_run else "applied"
    print(f"MIGRATIONS_{verb.upper()}={','.join(names) or '-'}")


if __name__ == "__main__":
    run()
