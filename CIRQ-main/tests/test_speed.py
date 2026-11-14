"""Page-speed basics: compression, cache headers for static files, and one badge query per page."""
import re


def test_text_responses_are_gzipped(client):
    r = client.get("/static/css/style.css", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers.get("Content-Encoding") == "gzip"
    assert len(r.data) < 60000                                              # ~180 KB of CSS shrinks to ~35 KB
    html = client.get("/login", headers={"Accept-Encoding": "gzip"})
    assert html.headers.get("Content-Encoding") == "gzip"
    assert client.get("/login").headers.get("Content-Encoding") is None      # browsers without gzip still work


def test_static_urls_are_versioned_and_cached_for_a_year(client):
    page = client.get("/login").get_data(as_text=True)
    css = re.search(r'href="(/static/css/style[^"]*\.css\?v=\d+)"', page)
    assert css, "stylesheet link should carry ?v=<file time>"
    versioned = client.get(css.group(1))
    assert "max-age=31536000" in versioned.headers["Cache-Control"] and "immutable" in versioned.headers["Cache-Control"]
    plain = client.get("/static/css/style.css")
    assert "max-age=3600" in plain.headers["Cache-Control"]                  # unversioned links are only kept an hour


def test_unread_message_badge_is_one_query_per_page(as_user, db, monkeypatch):
    a, _ = as_user("alice")
    seen = []
    from app.blueprints.messages import routes
    real = routes.unread_total
    monkeypatch.setattr(routes, "unread_total", lambda uid: (seen.append(uid), real(uid))[1])
    assert a.get("/notifications").status_code == 200
    assert len(seen) == 1
