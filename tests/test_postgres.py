import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.mark.postgres
def test_postgres_migration_and_row_locking():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgres://")
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")

    schema = f"movie_test_{uuid.uuid4().hex}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        test_url = make_url(url).update_query_dict(
            {"options": f"-csearch_path={schema}"}
        )
        environment = {
            **os.environ,
            "DATABASE_URL": test_url.render_as_string(hide_password=False),
            "SECRET_KEY": "postgres-integration-test",
        }
        root = Path(__file__).resolve().parents[1]
        command = [sys.executable, "-m", "flask", "--app", "movie_theater.py", "db"]
        subprocess.run(
            [*command, "upgrade"], cwd=root, env=environment, check=True
        )

        engine = create_engine(test_url)
        with engine.begin() as connection:
            tables = connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :schema"
                ),
                {"schema": schema},
            ).scalars().all()
            assert {"users", "watch_rooms", "room_videos", "room_memberships"} <= set(
                tables
            )
            connection.execute(
                text(
                    "INSERT INTO users (email, password_hash) "
                    "VALUES ('owner@example.com', 'hash')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO watch_rooms (code, name, owner_id) "
                    "VALUES ('ABCDEFGH', 'Room', 1)"
                )
            )

        first = engine.connect()
        first.begin()
        first.execute(text("SELECT id FROM watch_rooms WHERE id=1 FOR UPDATE"))
        second = engine.connect()
        second.begin()
        second.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(Exception):
            second.execute(text("SELECT id FROM watch_rooms WHERE id=1 FOR UPDATE"))
        second.rollback()
        second.close()
        first.rollback()
        first.close()

        subprocess.run(
            [*command, "check"], cwd=root, env=environment, check=True
        )
        subprocess.run(
            [*command, "downgrade", "base"],
            cwd=root,
            env=environment,
            check=True,
        )
        engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
