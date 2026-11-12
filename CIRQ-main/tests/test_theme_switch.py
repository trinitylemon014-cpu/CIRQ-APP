"""Dark (new, default) and light (the original) themes: a cookie-backed switch on the profile and settings pages."""
from pathlib import Path

from tests.conftest import login

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def theme_cookie(resp):
    return next((h for h in resp.headers.getlist("Set-Cookie") if h.startswith("cirq_theme=")), None)


def test_dark_is_the_default_everywhere(client):
    html = client.get("/").get_data(as_text=True)
    assert 'data-theme="dark"' in html and "css/style.css" in html and "style-light.css" not in html
    assert 'content="#0b0b0c"' in html and "images/favicon.png" in html


def test_choosing_light_sets_a_year_long_cookie_and_swaps_the_stylesheet(client, mk):
    mk("zoe"); login(client, "zoe")
    r = client.post("/theme", data={"theme": "light", "next": "/settings"})
    cookie = theme_cookie(r)
    assert r.status_code == 302 and r.headers["Location"].endswith("/settings")
    assert cookie and "cirq_theme=light" in cookie and "Max-Age=31536000" in cookie and "SameSite=Lax" in cookie and "HttpOnly" in cookie
    html = client.get("/settings").get_data(as_text=True)
    assert 'data-theme="light"' in html and "css/style-light.css" in html and "css/style.css" not in html.replace("style-light.css", "")
    assert 'content="#f6f6f1"' in html and "favicon-light.png" in html and "icon-192-light.png" in html


def test_switching_back_to_dark(client, mk):
    mk("zoe"); login(client, "zoe")
    client.post("/theme", data={"theme": "light", "next": "/feed"})
    client.post("/theme", data={"theme": "dark", "next": "/feed"})
    html = client.get("/feed").get_data(as_text=True)
    assert 'data-theme="dark"' in html and "style-light.css" not in html


def test_the_choice_also_applies_before_login(client):
    client.post("/theme", data={"theme": "light", "next": "/"})
    assert "css/style-light.css" in client.get("/login").get_data(as_text=True)
    assert "css/style-light.css" in client.get("/").get_data(as_text=True)


def test_a_junk_value_changes_nothing(client):
    r = client.post("/theme", data={"theme": "purple-neon", "next": "/"})
    assert theme_cookie(r) is None
    client.set_cookie("cirq_theme", "garbage")
    assert 'data-theme="dark"' in client.get("/").get_data(as_text=True)          # an unknown cookie falls back to dark


def test_next_can_never_be_an_open_redirect(client):
    for bad in ("https://evil.example/x", "//evil.example/x", "/\\evil.example", "javascript:alert(1)"):
        r = client.post("/theme", data={"theme": "light", "next": bad})
        assert r.status_code == 302 and "evil.example" not in r.headers["Location"] and not r.headers["Location"].startswith("javascript"), bad


def test_the_switch_is_on_your_own_profile_and_in_settings_with_the_active_side_marked(client, mk):
    mk("zoe"); mk("amy"); login(client, "zoe")
    mine = client.get("/u/zoe").get_data(as_text=True)
    assert 'class="theme-switch"' in mine and "Dark is the new look" in mine
    assert mine.count('aria-pressed="true"') >= 1 and 'name="theme" value="light"' in mine
    assert 'class="theme-switch"' not in client.get("/u/amy").get_data(as_text=True)         # nobody else's profile
    assert 'class="theme-switch"' in client.get("/settings").get_data(as_text=True)
    client.post("/theme", data={"theme": "light", "next": "/u/zoe"})
    light = client.get("/u/zoe").get_data(as_text=True)
    pressed = light.split('value="light"')[1].split("</button>")[0]
    assert 'aria-pressed="true"' in pressed                                              # the Light button is now the pressed one


def test_both_stylesheets_ship_with_the_icons_they_need(app):
    for name in ("css/style.css", "css/style-light.css", "images/favicon.png", "images/favicon-light.png", "images/icon-192.png", "images/icon-192-light.png"):
        assert (STATIC / name).stat().st_size > 1000, name
    dark, light = (STATIC / "css/style.css").read_text(), (STATIC / "css/style-light.css").read_text()
    assert "--brand: #f7b500" in dark and "--lime: #c9f900" in light                       # gold on black vs the original lime
    assert ".theme-switch" in dark and ".theme-switch" in light


def test_switch_buttons_post_back_to_the_same_page(client, mk):
    mk("zoe"); login(client, "zoe")
    page = client.get("/u/zoe?tab=reels").get_data(as_text=True)
    assert 'name="next" value="/u/zoe?tab=reels"' in page
