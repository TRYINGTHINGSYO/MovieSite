from sqlalchemy import select

from models import User, db


def test_registration_normalizes_email_hashes_password_and_logs_in(app, client):
    response = client.post(
        "/register",
        data={
            "email": "  Viewer@Example.COM ",
            "password": "correct-horse",
            "display_name": "Viewer",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/rooms")
    with app.app_context():
        user = db.session.scalar(select(User))
        assert user.email == "viewer@example.com"
        assert user.password_hash != "correct-horse"
        assert user.check_password("correct-horse")
    assert client.get("/rooms").status_code == 200


def test_duplicate_email_and_short_password_are_rejected(app, client, register):
    register()
    client.post("/logout")

    short = client.post(
        "/register",
        data={"email": "new@example.com", "password": "short"},
        follow_redirects=True,
    )
    duplicate = client.post(
        "/register",
        data={"email": "VIEWER@example.com", "password": "another-good-password"},
        follow_redirects=True,
    )

    assert b"at least 10 characters" in short.data
    assert b"already exists" in duplicate.data
    with app.app_context():
        assert len(db.session.scalars(select(User)).all()) == 1


def test_login_logout_and_unsafe_next_redirect(client, register):
    register(password="correct-horse")
    client.post("/logout")
    assert client.get("/rooms").status_code == 302

    failed = client.post(
        "/login",
        data={"email": "viewer@example.com", "password": "wrong-password"},
        follow_redirects=True,
    )
    assert b"Invalid email or password" in failed.data

    logged_in = client.post(
        "/login?next=https://attacker.example/",
        data={"email": "VIEWER@EXAMPLE.COM", "password": "correct-horse"},
    )
    assert logged_in.headers["Location"].endswith("/rooms")

    logout = client.post("/logout")
    assert logout.headers["Location"].endswith("/")
    assert client.get("/rooms").status_code == 302


def test_mutating_routes_require_csrf_when_enabled(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    response = client.post(
        "/register",
        data={"email": "viewer@example.com", "password": "correct-horse"},
    )
    assert response.status_code == 400
