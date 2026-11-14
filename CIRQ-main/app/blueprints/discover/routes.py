import time

from flask import Blueprint, render_template, request, g, current_app
from app.decorators import login_required
from app.extensions import db
from app.services import supabase_service as sb, social, discovery
from app.utils import iso, normalize_username

bp = Blueprint("discover", __name__)
_schools_cache = {"at": 0.0, "rows": None}


def _all_schools():
    """The school list for the filter dropdown. It almost never changes, so each worker keeps it for 2 minutes."""
    ttl = 0 if current_app.testing else 120
    if ttl and _schools_cache["rows"] is not None and time.monotonic() - _schools_cache["at"] < ttl:
        return _schools_cache["rows"]
    rows = sb.select("schools", order="name", desc=False)
    if ttl:
        _schools_cache.update(at=time.monotonic(), rows=rows)
    return rows


def _escape_like(q):
    return q.replace("%", "").replace("_", r"\_").replace(",", " ").replace("(", " ").replace(")", " ")


@bp.get("/discover")
@login_required
def index():
    q = (request.args.get("q") or "").strip()[:40]
    tab = request.args.get("tab", "people")
    me = g.user
    hidden = social.blocked_between(me["id"]) if tab in ("posts",) else set()
    people, schools, posts, events, acct = [], [], [], [], None
    if tab == "people":
        acct = discovery.accounts_page(me, request.args)
        if request.args.get("more") and acct["mode"] == "results":     # "Load more": just the next batch of cards
            return render_template("discover/_cards.html", acct=acct)
        if request.args.get("partial"):                                 # live search / chips: swap the body without a page load
            return render_template("discover/_accounts.html", acct=acct, all_schools=_all_schools())
    elif tab == "schools":
        query = db().table("schools").select("*").order("name").limit(60)
        if q:
            query = query.ilike("name", f"%{_escape_like(q)}%")
        schools = query.execute().data or []
    elif tab == "posts":
        rows = db().table("posts").select("*").eq("kind", "post").eq("visibility", "public").eq("moderation_status", "ok") \
            .is_("community_id", "null").order("like_count", desc=True).limit(24).execute().data or []
        hydrated = social.hydrate_posts(rows, me["id"])
        posts = [p for p in hydrated if p["author_id"] not in hidden and not p["author"].get("is_private")
                 and p["author"]["account_status"] == "approved"]
    elif tab == "events":
        query = db().table("events").select("*").eq("moderation_status", "ok").eq("status", "scheduled") \
            .gt("starts_at", iso()).order("starts_at").limit(24)
        if q:
            query = query.ilike("title", f"%{_escape_like(q)}%")
        events = [e for e in (query.execute().data or []) if e["visibility"] == "public" or e.get("school_id") == me.get("school_id")]
    all_schools = _all_schools()
    return render_template("discover/index.html", tab=tab, q=q, acct=acct, people=people, schools=schools, posts=posts, events=events,
                           all_schools=all_schools, sel_school=request.args.get("school", ""))
