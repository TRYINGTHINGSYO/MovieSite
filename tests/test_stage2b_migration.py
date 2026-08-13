import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect


STAGE2A_REVISION = "b7d9a4c2e6f1"


def run_migration(root, environment, *args):
    subprocess.run(
        [sys.executable, "-m", "flask", "--app", "movie_theater.py", "db", *args],
        cwd=root,
        env=environment,
        check=True,
    )


def test_stage2b_receipt_migration_is_independent_and_reversible(tmp_path):
    database_path = tmp_path / "stage2b-migration.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "SECRET_KEY": "stage2b-migration-test",
    }
    run_migration(root, environment, "upgrade", STAGE2A_REVISION)
    engine = create_engine(database_url)
    assert "room_command_receipts" not in inspect(engine).get_table_names()

    run_migration(root, environment, "upgrade")
    inspector = inspect(engine)
    assert "room_command_receipts" in inspector.get_table_names()
    assert "probe_result" in {
        column["name"] for column in inspector.get_columns("direct_url_sources")
    }
    assert "ck_direct_url_sources_probe_result" in {
        constraint["name"]
        for constraint in inspector.get_check_constraints("direct_url_sources")
    }
    unique_columns = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("room_command_receipts")
    }
    assert ("room_id", "actor_key", "client_action_id") in unique_columns

    run_migration(root, environment, "downgrade", STAGE2A_REVISION)
    assert "room_command_receipts" not in inspect(engine).get_table_names()
    assert "probe_result" not in {
        column["name"] for column in inspect(engine).get_columns("direct_url_sources")
    }
    assert "room_requests" in inspect(engine).get_table_names()
    engine.dispose()
