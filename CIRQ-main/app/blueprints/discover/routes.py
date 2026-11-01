from flask import Blueprint, render_template, request, g, current_app
from app.decorators import login_required
from app.extensions import db
from app.services import supabase_service as sb, social
from app.services.coin_service import is_boosted
from app.utils import iso, normalize_username

bp = Blueprint("discover", __name__)


def _escape_like(q):
    return q.replace("%", "").replace("_", r"\_").replace(",", " ").replace("(", " ").replace(")", " ")


@bp.get("/discover")
@login_required
def index():
    q = (request.args.get("q") or "").strip()[:40]
    tab = request.args.get("tab", "people")
    me = g.user
    hidden = social.blocked_between(me["id"])
    people, schools, posts, events = [], [], [], []
    if tab == "people":
        query = db().table("profiles").select("*").eq("account_status", "approved").limit(60)
        if q:
            term = _escape_like(q)
            query = query.or_(f"username.ilike.%{term}%,display_name.ilike.%{term}%")
        if request.args.get("school"):
            query = query.eq("school_id", request.args["school"])
        rows = [p for p in (query.execute().data or []) if p["id"] != me["id"] and p["id"] not in hidden]
        # Boosted students are shown first and are labelled "Boosted"; everyone else is ordered by newest.
        rows.sort(key=lambda p: not is_boosted(p))
        people = rows[:30]
        follows, asked = set(social.following_ids(me["id"])), social.requested_ids(me["id"])
        for p in people:
            p["_state"] = "following" if p["id"] in follows else ("requested" if p["id"] in asked else "none")
        sch = sb.by_id("schools", [p.get("school_id") for p in people])
        for p in people:
            p["_school"] = sch.get(p.get("school_id"))
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
    all_schools = sb.select("schools", order="name", desc=False)
    return render_template("discover/index.html", tab=tab, q=q, people=people, schools=schools, posts=posts, events=events,
                           all_schools=all_schools, sel_school=request.args.get("school", ""))
