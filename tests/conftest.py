import os

import pytest
from sqlalchemy import event

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "test-secret"

from models import db
from movie_theater import (
    app as flask_app,
    identity_join_failures,
    limiter,
    revoked_sids,
    sid_to_browser_client,
    sid_to_code,
    sid_to_guest,
    sid_to_presence,
    sid_to_user,
    user_to_sids,
    viewer_counts,
)


def enable_foreign_keys(connection, _record):
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


with flask_app.app_context():
    event.listen(db.engine, "connect", enable_foreign_keys)


@pytest.fixture
def app():
    flask_app.config.update(
        TESTING=True,
        SECRET_KEY="test-secret",
        WTF_CSRF_ENABLED=False,
        RATELIMIT_ENABLED=False,
    )
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
    limiter.reset()
    sid_to_code.clear()
    sid_to_browser_client.clear()
    sid_to_guest.clear()
    identity_join_failures.clear()
    sid_to_presence.clear()
    sid_to_user.clear()
    user_to_sids.clear()
    revoked_sids.clear()
    viewer_counts.clear()
    yield flask_app
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def register(client):
    def register_user(
        email="viewer@example.com", password="correct-horse", display_name="Viewer"
    ):
        return client.post(
            "/register",
            data={
                "email": email,
                "password": password,
                "display_name": display_name,
            },
            follow_redirects=True,
        )

    return register_user
