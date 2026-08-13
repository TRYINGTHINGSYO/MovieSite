import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text


STAGE1_REVISION = "ea92f8b3bf52"


def _flask_db(root, environment, *args):
    subprocess.run(
        [sys.executable, "-m", "flask", "--app", "movie_theater.py", "db", *args],
        cwd=root,
        env=environment,
        check=True,
    )


def test_stage1_media_backfills_and_expand_downgrade_preserves_legacy(tmp_path):
    database_path = tmp_path / "stage2-migration.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "SECRET_KEY": "stage2-migration-test",
    }

    _flask_db(root, environment, "upgrade", STAGE1_REVISION)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, email, password_hash) "
                "VALUES (1, 'owner@example.com', 'hash'), "
                "(2, 'member@example.com', 'hash')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO watch_rooms "
                "(id, code, name, owner_id, playing, position) "
                "VALUES (1, 'ABCDEFGH', 'Migrated room', 1, 1, 37.5)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO room_memberships (room_id, user_id) "
                "VALUES (1, 1), (1, 2)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO room_videos "
                "(id, room_id, name, sort_order, status, mux_upload_id, "
                "asset_id, playback_id, url, duration, created_by_id) VALUES "
                "('video-ready', 1, 'Ready', 0, 'ready', 'upload-ready', "
                "'asset-ready', 'playback-ready', "
                "'https://stream.mux.com/playback-ready.m3u8', 100, 1), "
                "('video-current', 1, 'Processing', 1, 'processing', "
                "'upload-current', 'asset-current', NULL, NULL, NULL, 2), "
                "('video-error', 1, 'Error', 2, 'error', 'upload-error', "
                "NULL, NULL, NULL, NULL, 1)"
            )
        )
        connection.execute(
            text(
                "UPDATE watch_rooms SET current_video_id = 'video-current' "
                "WHERE id = 1"
            )
        )

    _flask_db(root, environment, "upgrade")
    with engine.connect() as connection:
        room = connection.execute(
            text(
                "SELECT current_video_id, current_queue_entry_id, playing, position, "
                "queue_version, playback_version FROM watch_rooms WHERE id = 1"
            )
        ).mappings().one()
        assert room["current_video_id"] == "video-current"
        assert room["current_queue_entry_id"] == "video-current"
        assert bool(room["playing"]) is True
        assert room["position"] == 37.5
        assert room["queue_version"] == 0
        assert room["playback_version"] == 0

        queue = connection.execute(
            text(
                "SELECT id, room_media_id, position FROM queue_entries "
                "ORDER BY position"
            )
        ).mappings().all()
        assert queue == [
            {"id": "video-ready", "room_media_id": "video-ready", "position": 0},
            {"id": "video-current", "room_media_id": "video-current", "position": 1},
            {"id": "video-error", "room_media_id": "video-error", "position": 2},
        ]

        sources = connection.execute(
            text(
                "SELECT id, source_type, status FROM media_sources ORDER BY id"
            )
        ).mappings().all()
        assert sources == [
            {"id": "video-current", "source_type": "mux_upload", "status": "processing"},
            {"id": "video-error", "source_type": "mux_upload", "status": "error"},
            {"id": "video-ready", "source_type": "mux_upload", "status": "ready"},
        ]
        mux_ready = connection.execute(
            text(
                "SELECT upload_id, asset_id, playback_id FROM mux_media_sources "
                "WHERE source_id = 'video-ready'"
            )
        ).mappings().one()
        assert mux_ready == {
            "upload_id": "upload-ready",
            "asset_id": "asset-ready",
            "playback_id": "playback-ready",
        }
        assert connection.execute(
            text("SELECT COUNT(*) FROM room_member_permissions")
        ).scalar_one() == 0
        assert connection.execute(
            text("SELECT COUNT(*) FROM room_videos")
        ).scalar_one() == 3

    _flask_db(root, environment, "check")
    _flask_db(root, environment, "downgrade", STAGE1_REVISION)
    inspector = inspect(engine)
    assert "queue_entries" not in inspector.get_table_names()
    with engine.connect() as connection:
        legacy = connection.execute(
            text(
                "SELECT current_video_id, playing, position FROM watch_rooms WHERE id = 1"
            )
        ).mappings().one()
        assert legacy["current_video_id"] == "video-current"
        assert bool(legacy["playing"]) is True
        assert legacy["position"] == 37.5
        assert connection.execute(
            text("SELECT COUNT(*) FROM room_videos")
        ).scalar_one() == 3
    engine.dispose()


def test_stage2_migration_preflight_rejects_duplicate_mux_ownership(tmp_path):
    database_path = tmp_path / "stage2-duplicate-preflight.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "SECRET_KEY": "stage2-duplicate-test",
    }
    _flask_db(root, environment, "upgrade", STAGE1_REVISION)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, email, password_hash) "
                "VALUES (1, 'owner@example.com', 'hash')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO watch_rooms (id, code, name, owner_id) "
                "VALUES (1, 'ABCDEFGH', 'Room', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO room_videos "
                "(id, room_id, name, sort_order, status, mux_upload_id, asset_id) "
                "VALUES ('one', 1, 'One', 0, 'ready', 'upload-one', 'duplicate'), "
                "('two', 1, 'Two', 1, 'ready', 'upload-two', 'duplicate')"
            )
        )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "flask",
            "--app",
            "movie_theater.py",
            "db",
            "upgrade",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "duplicate Mux asset_id" in result.stderr
    assert "media_assets" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM room_videos")
        ).scalar_one() == 2
    engine.dispose()


def test_stage2_downgrade_refuses_to_discard_permission_changes(tmp_path):
    database_path = tmp_path / "stage2-downgrade-guard.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "SECRET_KEY": "stage2-downgrade-test",
    }
    _flask_db(root, environment, "upgrade")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, email, password_hash) VALUES "
                "(1, 'owner@example.com', 'hash'), (2, 'member@example.com', 'hash')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO watch_rooms (id, code, name, owner_id) "
                "VALUES (1, 'ABCDEFGH', 'Room', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO room_memberships (room_id, user_id) VALUES (1, 1), (1, 2)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO room_member_permissions "
                "(room_id, user_id, permission, granted_by_id) "
                "VALUES (1, 2, 'CONTROL_PLAYBACK', 1)"
            )
        )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "flask",
            "--app",
            "movie_theater.py",
            "db",
            "downgrade",
            STAGE1_REVISION,
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "member permissions exist" in result.stderr
    assert "room_member_permissions" in inspect(engine).get_table_names()
    engine.dispose()
