from tests.conftest import login


def test_create_admin_cli_creates_working_admin(app, db):
    runner = app.test_cli_runner()
    res = runner.invoke(args=["create-admin"], input="Boss\nboss@example.com\nThe Boss\nStr0ng-pass-phrase\nStr0ng-pass-phrase\n")
    assert res.exit_code == 0, res.output
    p = db.data["profiles"][0]
    assert p["role"] == "admin" and p["account_status"] == "approved" and "password" not in p
    c = app.test_client(); assert login(c, "boss", "Str0ng-pass-phrase").status_code == 302
    assert c.get("/admin").status_code == 200


def test_create_admin_cli_rejects_weak_password(app, db):
    res = app.test_cli_runner().invoke(args=["create-admin"], input="boss\nb@example.com\nB\nshort\nshort\n")
    assert res.exit_code != 0 and not db.data.get("profiles")


def test_cleanup_cli_runs(app, db):
    assert "removed 0 expired stories" in app.test_cli_runner().invoke(args=["cleanup-expired"]).output
