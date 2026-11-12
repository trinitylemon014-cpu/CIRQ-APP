"""The dark theme is flat black and gold: no decorative gradients, no neon glows, one gold accent."""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "style.css").read_text(encoding="utf-8")
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
# a dark fade behind text on top of a photo or video is for legibility, not decoration
SCRIMS = {".reel .shade", ".sponsored-reel-shade", ".auth-art::after", ".land-art::after", ".cam-top", ".cam-bottom", ".saved-shade"}


def rules():
    for m in RULE.finditer(re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)):
        yield " ".join(m.group(1).split()), m.group(2)


def test_no_gradients_except_scrims_over_photos_and_video():
    bad = sorted({sel for sel, body in rules() if "gradient(" in body and not all(s.strip() in SCRIMS for s in sel.split(","))})
    assert bad == [], f"decorative gradients found: {bad}"


def test_gradient_tokens_are_solid_colours():
    tokens = dict(re.findall(r"(--grad-[a-z]+):\s*([^;]+);", CSS))
    assert tokens and all("gradient" not in v for v in tokens.values()), tokens


def test_the_accent_is_gold_and_the_old_lime_and_neon_are_gone():
    assert re.search(r"--brand:\s*#f7b500", CSS)
    for old in ("#c9f900", "201, 249, 0", "#8b2cff", "139, 44, 255", "#00d9ff", "0, 217, 255", "#d62bff"):
        assert old not in CSS.replace("--purple: #f7b500", ""), f"old neon/lime colour still present: {old}"


def test_no_glow_shadows():
    glows = [(s, b) for s, b in rules() for d in b.split(";") if d.strip().startswith(("box-shadow", "filter")) and "drop-shadow(0 0" in d]
    assert glows == []


def test_page_chrome_is_dark(client):
    html = client.get("/").get_data(as_text=True)
    assert 'content="#0b0b0c"' in html
