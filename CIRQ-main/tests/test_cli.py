from tests.conftest import login


def test_create_admin_cli_creates_working_admin(app, db):
    runner = app.test_cli_runner()
    res = runner.invoke(args=["create-admin"], input="Boss\nboss@example.com\nThe Boss\nBoss\nBoss\n")
    assert res.exit_code == 0, res.output
    p = db.data["profiles"][0]
    assert p["role"] == "admin" and p["account_status"] == "approved" and "password" not in p
    c = app.test_client(); assert login(c, "boss", "Boss").status_code == 302
    assert c.get("/admin").status_code == 200


def test_create_admin_cli_accepts_short_password(app, db):
    res = app.test_cli_runner().invoke(args=["create-admin"], input="boss\nb@example.com\nB\nx\nx\n")
    assert res.exit_code == 0, res.output
    assert db.data["profiles"][0]["username"] == "boss"


def test_cleanup_cli_runs(app, db):
    assert "removed 0 expired stories" in app.test_cli_runner().invoke(args=["cleanup-expired"]).output
