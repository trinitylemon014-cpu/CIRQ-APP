"""A small in-memory stand-in for the Supabase client, used ONLY by the test-suite.

It implements the subset of the PostgREST query builder and Supabase Auth admin API that CIRQ uses, plus the
database-side behaviour that lives in schema.sql (defaults, unique constraints, counter triggers and the
cirq_apply_coins function). It is not a substitute for testing against a real Supabase project.
"""
import copy
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

NOW = lambda: datetime.now(timezone.utc).isoformat()

DEFAULTS = {
    "profiles": dict(role="student", account_status="pending", verified=False, is_private=False, show_active_status=True,
                     last_seen_at=None, session_epoch=0, coin_balance=0,
                     boosted_until=None, personalized_recs=True, email_verified_at=None, bio=None, avatar_url=None, avatar_public_id=None, school_id=None, status_note=None,
                     school_verified=False, submitted_school_name=None, school_submission_id=None, auto_approval_blocked=False,
                     approval_source=None, approved_by=None, approved_at=None),
    "posts": dict(kind="post", visibility="public", moderation_status="ok", like_count=0, comment_count=0, views_count=0, is_announcement=False,
                  community_id=None, edited_at=None, caption=None,
                  audio_url=None, audio_public_id=None, audio_title=None, audio_duration=None),
    "comments": dict(moderation_status="ok"),
    "stories": dict(moderation_status="ok", delivery_type="upload", caption=None,
                    audio_url=None, audio_public_id=None, audio_title=None, audio_duration=None),
    "story_comments": dict(moderation_status="ok"),
    "events": dict(status="scheduled", visibility="public", moderation_status="ok", capacity=None, conversation_id=None, description=None,
                   cover_url=None, cover_public_id=None, ends_at=None, school_id=None),
    "messages": dict(moderation_status="ok", body=None, media_type=None, media_url=None, media_public_id=None, story_id=None, story_reply=False),
    "reports": dict(status="open", resolved_by=None, resolution_note=None, resolved_at=None, details=None),
    "password_reset_requests": dict(status="pending", manual_review=False, token_hash=None, token_expires_at=None, token_used_at=None,
                                    reviewed_by=None, reviewed_at=None, decision_note=None, completed_at=None, submitted_details={}),
    "payer_numbers": dict(status="pending", network="mobile_money", guardian_confirmed=False, reviewed_by=None, reviewed_at=None, note=None),
    "trivia_sets": dict(status="draft", description=None, published_at=None),
    "trivia_attempts": dict(status="in_progress", paid=False, current_position=1, correct_count=0, total_questions=0, accuracy=0,
                            points=0, coins_awarded=0, completed_at=None),
    "trivia_entry_credits": dict(used_set_id=None, used_at=None, payment_id=None),
    "game_rounds": dict(status="active", guesses_used=0, ended_at=None),
    "conversation_members": dict(last_read_at=None),
    "notifications": dict(read_at=None, actor_id=None, ref_type=None, ref_id=None),
    "schools": dict(verified=False, country="Uganda", city=None, about=None, logo_url=None, logo_public_id=None),
    "school_submissions": dict(status="pending", submitted_by=None, resolved_school_id=None, reviewed_by=None, reviewed_at=None, decision_note=None),
    "school_verification_requests": dict(status="pending", reviewed_by=None, reviewed_at=None, decision_note=None),
    "community_members": dict(role="member"),
    "feature_flags": dict(enabled=False),
    "developer_card_settings": dict(display_name=None, image_url=None, image_public_id=None, updated_by=None),
    "boost_ads": {},
    "email_verifications": dict(used_at=None),
    "post_media": dict(delivery_type="upload"),
    "coin_gifts": dict(message=None),
    "coin_loans": dict(message=None, status="offered", accepted_at=None, fee_amount=0, repaid_total=0, forgiven_amount=0, overdue_at=None, closed_at=None, frozen=False),
    "coin_loan_events": dict(amount=0, actor_id=None, note=None),
}
PKS = {"likes": ("post_id", "user_id"), "saved_posts": ("post_id", "user_id"), "follows": ("follower_id", "followee_id"), "blocks": ("blocker_id", "blocked_id"),
       "community_members": ("community_id", "user_id"), "conversation_members": ("conversation_id", "user_id"),
       "event_rsvps": ("event_id", "user_id"), "user_interests": ("user_id", "dim", "key"), "story_views": ("story_id", "viewer_id"), "story_likes": ("story_id", "user_id"), "comment_likes": ("comment_id", "user_id"), "story_comment_likes": ("comment_id", "user_id"), "follow_requests": ("requester_id", "target_id"), "user_achievements": ("user_id", "code"),
       "feature_flags": ("key",), "developer_card_settings": ("id",), "achievements": ("code",),
       "school_submission_rate_limits": ("ip_hash",)}
UNIQUES = {"coin_loan_events": [("loan_id", "ref")], "profiles": [("username",)], "schools": [("slug",), ("name",)], "game_guesses": [("round_id", "card_index")],
           "reel_views": [("reel_id", "viewer_id")],
           "boost_ads": [("user_id",)],
           "trivia_answers": [("attempt_id", "question_id")], "trivia_attempts": [("set_id", "user_id")],
           "payments": [("provider_reference",)], "conversations": [("dm_key",)], "reports": [("reporter_id", "target_type", "target_id")],
           "trivia_questions": [("set_id", "position")], "communities": [("school_id",)]}


def _match(row, filters):
    for kind, col, val in filters:
        v = row.get(col)
        if kind == "eq" and v != val: return False
        if kind == "neq" and v == val: return False
        if kind == "is" and v is not None: return False
        if kind == "in" and v not in val: return False
        if kind in ("gt", "gte", "lt", "lte"):
            if v is None: return False
            if kind == "gt" and not v > val: return False
            if kind == "gte" and not v >= val: return False
            if kind == "lt" and not v < val: return False
            if kind == "lte" and not v <= val: return False
        if kind == "ilike":
            needle = val.replace("%", "").lower()
            if needle not in str(v or "").lower(): return False
        if kind == "or":
            if not any(_match(row, [f]) for f in val): return False
        if kind == "and":
            if not all(_match(row, [f]) for f in val): return False
    return True


def _split_filters(expression):
    parts, depth, start = [], 0, 0
    for i, char in enumerate(expression):
        if char == "(": depth += 1
        elif char == ")": depth -= 1
        elif char == "," and depth == 0:
            parts.append(expression[start:i])
            start = i + 1
    parts.append(expression[start:])
    return parts


def _parse_filter(expression):
    expression = expression.strip()
    if expression.startswith("and(") and expression.endswith(")"):
        return ("and", None, [_parse_filter(p) for p in _split_filters(expression[4:-1])])
    col, op, val = expression.split(".", 2)
    if op == "in" and val.startswith("(") and val.endswith(")"):
        val = [item for item in val[1:-1].split(",") if item]
    return (op, col, val)


class Query:
    def __init__(self, db, table):
        self.db, self.table, self.op, self.filters = db, table, "select", []
        self.payload, self.cols, self.want_count, self._order, self._limit, self.on_conflict = None, "*", False, [], None, None

    # builders
    def select(self, cols="*", count=None, **_): self.op, self.cols, self.want_count = "select", cols, bool(count); return self
    def insert(self, row): self.op, self.payload = "insert", row; return self
    def update(self, values): self.op, self.payload = "update", values; return self
    def delete(self): self.op = "delete"; return self
    def upsert(self, row, on_conflict=None): self.op, self.payload, self.on_conflict = "upsert", row, on_conflict; return self
    def eq(self, c, v): self.filters.append(("eq", c, v)); return self
    def neq(self, c, v): self.filters.append(("neq", c, v)); return self
    def gt(self, c, v): self.filters.append(("gt", c, v)); return self
    def gte(self, c, v): self.filters.append(("gte", c, v)); return self
    def lt(self, c, v): self.filters.append(("lt", c, v)); return self
    def lte(self, c, v): self.filters.append(("lte", c, v)); return self
    def in_(self, c, v): self.filters.append(("in", c, list(v))); return self
    def is_(self, c, v): self.filters.append(("is", c, None)); return self
    def ilike(self, c, v): self.filters.append(("ilike", c, v)); return self
    def or_(self, expr):
        self.filters.append(("or", None, [_parse_filter(p) for p in _split_filters(expr)])); return self
    def order(self, col, desc=False): self._order.append((col, desc)); return self
    def limit(self, n): self._limit = n; return self
    def range(self, a, b): self._limit = b - a + 1; return self

    def execute(self):
        rows = self.db.data.setdefault(self.table, [])
        if self.op == "insert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = [self.db._insert(self.table, i) for i in items]
            return SimpleNamespace(data=copy.deepcopy(out), count=None)
        if self.op == "upsert" and isinstance(self.payload, list):
            out = []
            for item in self.payload:
                out += Query(self.db, self.table).upsert(item, self.on_conflict).execute().data
            return SimpleNamespace(data=out, count=None)
        if self.op == "upsert":
            keys = self.on_conflict.split(",") if self.on_conflict else list(PKS.get(self.table, ("id",)))
            ex = next((r for r in rows if all(r.get(k) == self.payload.get(k) for k in keys)), None)
            if ex:
                ex.update(self.payload); return SimpleNamespace(data=[copy.deepcopy(ex)], count=None)
            return SimpleNamespace(data=[copy.deepcopy(self.db._insert(self.table, self.payload))], count=None)
        hit = [r for r in rows if _match(r, self.filters)]
        if self.op == "update":
            for r in hit: r.update(copy.deepcopy(self.payload))
            return SimpleNamespace(data=copy.deepcopy(hit), count=None)
        if self.op == "delete":
            for r in hit: self.db._cascade(self.table, r)
            self.db.data[self.table] = [r for r in rows if r not in hit]
            return SimpleNamespace(data=copy.deepcopy(hit), count=None)
        total = len(hit)
        for c, d in reversed(self._order):
            hit = sorted(hit, key=lambda r: (r.get(c) is None, r.get(c)), reverse=d)
        if self._limit is not None: hit = hit[: self._limit]
        out = copy.deepcopy(hit)
        if self.cols not in ("*", "") and "(" not in self.cols:
            names = [c.strip() for c in self.cols.split(",")]
            out = [{k: r.get(k) for k in names} for r in out]
        return SimpleNamespace(data=out, count=total if self.want_count else None)


class FakeAuth:
    def __init__(self, db): self.db = db


class FakeAdmin:
    def __init__(self, db): self.db = db
    def create_user(self, attrs):
        email = attrs["email"].strip().lower()
        if any(u["email"].lower() == email for u in self.db.users.values()):
            raise Exception("User already registered")
        uid = str(uuid.uuid4()); self.db.users[uid] = {"email": email, "password": attrs["password"]}
        return SimpleNamespace(user=SimpleNamespace(id=uid, email=email))
    def get_user_by_id(self, uid):
        u = self.db.users[uid]; return SimpleNamespace(user=SimpleNamespace(id=uid, email=u["email"]))
    def update_user_by_id(self, uid, attrs):
        update = {k: v for k, v in attrs.items() if k in ("email", "password")}
        if "email" in update:
            update["email"] = update["email"].strip().lower()
        self.db.users[uid].update(update)
    def delete_user(self, uid):
        self.db.users.pop(uid, None)
        self.db.data["profiles"] = [p for p in self.db.data.get("profiles", []) if p["id"] != uid]


class FakeDB:
    def __init__(self):
        self.data, self.users = {}, {}
        self.auth = SimpleNamespace(admin=FakeAdmin(self))
        self.destroyed = []

    def table(self, name): return Query(self, name)

    def _insert(self, table, item):
        row = {**DEFAULTS.get(table, {}), **copy.deepcopy(item)}
        if table not in PKS and table not in ("achievements",):
            row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("created_at", NOW())
        if table == "conversation_members" and row.get("last_read_at") is None: row["last_read_at"] = NOW()
        rows = self.data.setdefault(table, [])
        if table in PKS:
            if any(all(r.get(k) == row.get(k) for k in PKS[table]) for r in rows): raise Exception(f"duplicate key on {table}")
        for cols in UNIQUES.get(table, []):
            if any(all(r.get(c) == row.get(c) for c in cols) for r in rows): raise Exception(f"unique violation on {table}{cols}")
        if table == "school_submissions" and row["status"] == "pending" and any(
                r["status"] == "pending" and r["normalized_name"] == row["normalized_name"] for r in rows):
            error = Exception("duplicate pending school submission")
            error.code = "23505"
            raise error
        if table == "game_rounds" and row["status"] == "active" and any(r["user_id"] == row["user_id"] and r["status"] == "active" for r in rows):
            raise Exception("one active round per user")
        if table == "payer_numbers" and any(r["phone"] == row["phone"] and r["status"] in ("pending", "approved") for r in rows):
            raise Exception("phone already registered")
        rows.append(row)
        post = {"likes": ("posts", "like_count", 1), "comments": ("posts", "comment_count", 1)}.get(table)
        if post:                                        # mirrors cirq_bump_post_counters()
            for p in self.data.get("posts", []):
                if p["id"] == row["post_id"]: p[post[1]] += 1
        return row

    def _cascade(self, table, row):
        if table in ("likes", "comments"):
            for p in self.data.get("posts", []):
                if p["id"] == row["post_id"]: p["like_count" if table == "likes" else "comment_count"] = max(0, p["like_count" if table == "likes" else "comment_count"] - 1)
        if table == "posts":
            for t in ("post_media", "likes", "comments", "saved_posts"):
                self.data[t] = [r for r in self.data.get(t, []) if r.get("post_id") != row["id"]]

    def rpc(self, name, params):
        if name == "cirq_profile_id_for_email":
            email = params["p_email"].strip().lower()
            row = next((p for uid, user in self.users.items()
                        if user["email"].lower() == email
                        for p in self.data.get("profiles", []) if p["id"] == uid), None)
            data = [{"user_id": row["id"]}] if row else []
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
        if name == "cirq_ping_presence":
            from datetime import datetime, timedelta, timezone
            row = next((p for p in self.data.get("profiles", []) if p["id"] == params["p_user_id"]), None)
            updated = False
            if row and row.get("show_active_status", True):
                last_seen = datetime.fromisoformat(row["last_seen_at"].replace("Z", "+00:00")) if row.get("last_seen_at") else None
                if last_seen is None or last_seen <= datetime.now(timezone.utc) - timedelta(seconds=30):
                    row["last_seen_at"] = NOW()
                    updated = True
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=updated))
        if name == "cirq_claim_school_submission_slot":
            from datetime import datetime, timezone
            day = datetime.now(timezone.utc).date().isoformat()
            ip_hash, limit = params["p_ip_hash"], params["p_limit"]
            rows = self.data.setdefault("school_submission_rate_limits", [])
            bucket = next((r for r in rows if r["ip_hash"] == ip_hash), None)
            if not bucket:
                rows.append({"ip_hash": ip_hash, "submission_date": day, "attempts": 1})
                allowed = True
            elif bucket["submission_date"] != day:
                bucket.update(submission_date=day, attempts=1)
                allowed = True
            elif bucket["attempts"] < limit:
                bucket["attempts"] += 1
                allowed = True
            else:
                allowed = False
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=allowed))
        if name == "cirq_record_reel_view":
            reel_id, viewer_id = params["p_reel_id"], params["p_viewer_id"]
            post = next((p for p in self.data.get("posts", []) if p["id"] == reel_id and p["kind"] == "reel"), None)
            if not post:
                return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))
            exists = any(v["reel_id"] == reel_id and v["viewer_id"] == viewer_id for v in self.data.get("reel_views", []))
            counted = not exists and post["author_id"] != viewer_id
            if counted:
                self._insert("reel_views", {"reel_id": reel_id, "viewer_id": viewer_id})
                post["views_count"] = post.get("views_count", 0) + 1
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=[{
                "counted": counted, "views_count": post.get("views_count", 0),
            }]))
        if name in ("cirq_discover_pool", "cirq_follower_counts"):
            if not getattr(self, "discover_rpc", True):          # tests can switch the migration "off" to exercise the fallback
                raise RuntimeError("function public.%s does not exist" % name)
            approved = {p["id"] for p in self.data.get("profiles", []) if p.get("account_status") == "approved"}
            agg = {}
            for f in self.data.get("follows", []):
                if f["followee_id"] in approved:
                    a = agg.setdefault(f["followee_id"], [0, 0])
                    a[0] += 1
                    if name == "cirq_discover_pool" and f["created_at"] >= params["p_since"]:
                        a[1] += 1
            if name == "cirq_follower_counts":
                rows = [{"profile_id": i, "followers": agg[i][0]} for i in params["p_ids"] if i in agg]
            else:
                pop = sorted(agg.items(), key=lambda kv: -kv[1][0])[:params["p_limit"]]
                rise = sorted([kv for kv in agg.items() if kv[1][1] > 0], key=lambda kv: (-kv[1][1], -kv[1][0]))[:params["p_limit"]]
                rows = [{"profile_id": k, "followers": v[0], "recent": v[1]} for k, v in {**dict(pop), **dict(rise)}.items()]
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=rows))
        if name == "cirq_transfer_coins":
            a, b, amt = params["p_from"], params["p_to"], params["p_amount"]
            ref = params["p_ref"] or ""
            res = lambda v: SimpleNamespace(execute=lambda: SimpleNamespace(data=v))
            if not amt or amt <= 0 or a == b: return res(-3)
            src = next(p for p in self.data["profiles"] if p["id"] == a)
            dst = next(p for p in self.data["profiles"] if p["id"] == b)
            if src["coin_balance"] < amt: return res(-2)
            led = self.data.setdefault("coin_ledger", [])
            if any(r["user_id"] == a and r["reason"] == params["p_reason_out"] and r["ref"] == ref for r in led): return res(-1)
            led.append({"id": str(uuid.uuid4()), "user_id": a, "amount": -amt, "reason": params["p_reason_out"], "ref": ref, "created_at": NOW()})
            led.append({"id": str(uuid.uuid4()), "user_id": b, "amount": amt, "reason": params["p_reason_in"], "ref": ref, "created_at": NOW()})
            src["coin_balance"] -= amt; dst["coin_balance"] += amt
            return res(src["coin_balance"])
        assert name == "cirq_apply_coins"
        u, amt, reason, ref = params["p_user"], params["p_amount"], params["p_reason"], params["p_ref"] or ""
        prof = next(p for p in self.data["profiles"] if p["id"] == u)
        if amt < 0 and prof["coin_balance"] + amt < 0: return SimpleNamespace(execute=lambda: SimpleNamespace(data=-2))
        led = self.data.setdefault("coin_ledger", [])
        if any(r["user_id"] == u and r["reason"] == reason and r["ref"] == ref for r in led): return SimpleNamespace(execute=lambda: SimpleNamespace(data=-1))
        led.append({"id": str(uuid.uuid4()), "user_id": u, "amount": amt, "reason": reason, "ref": ref, "created_at": NOW()})
        prof["coin_balance"] += amt
        bal = prof["coin_balance"]
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=bal))


def seed_from_schema(db, path="schema.sql"):
    """Load the achievement and feature-flag seed rows straight from schema.sql so tests and schema can't drift."""
    import re, os
    text = open(os.path.join(os.path.dirname(__file__), "..", path)).read()
    for code, name, desc, icon in re.findall(r"\('(\w+)',\s*'([^']+)',\s*'([^']+)',\s*'(\w+)'\)", text.split("insert into achievements")[1]):
        db.table("achievements").insert({"code": code, "name": name, "description": desc, "icon": icon}).execute()
    for key, en in re.findall(r"\('(\w+)',\s*(true|false),", text.split("insert into feature_flags")[1].split("on conflict")[0]):
        db.table("feature_flags").insert({"key": key, "enabled": en == "true"}).execute()
    db.table("developer_card_settings").insert({"id": "developer"}).execute()


class FakeAnon:
    def __init__(self, db):
        def sign_in(creds):
            for uid, u in db.users.items():
                if u["email"].lower() == creds["email"].lower() and u["password"] == creds["password"]:
                    return SimpleNamespace(user=SimpleNamespace(id=uid))
            raise Exception("Invalid login credentials")
        self.auth = SimpleNamespace(sign_in_with_password=sign_in)
