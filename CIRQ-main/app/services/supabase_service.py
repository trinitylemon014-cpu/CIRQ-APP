"""Thin data-access helpers over the Supabase (PostgREST) client.

All queries run server-side with the service-role key, so every route must do its own
authorization. Row Level Security in schema.sql is a second layer that protects data if the
anon key is ever used directly.
"""
from supabase import create_client
from app.extensions import db


def make_client(url, key):
    return create_client(url, key)


def _filter(q, eq):
    """eq filters; None means IS NULL (PostgREST would otherwise match the text 'None')."""
    for k, v in eq.items():
        q = q.is_(k, "null") if v is None else q.eq(k, v)
    return q


def select(table, cols="*", order=None, desc=True, limit=None, **eq):
    q = _filter(db().table(table).select(cols), eq)
    if order:
        q = q.order(order, desc=desc)
    if limit:
        q = q.limit(limit)
    return q.execute().data or []


def get(table, cols="*", **eq):
    rows = select(table, cols, limit=1, **eq)
    return rows[0] if rows else None


def insert(table, row):
    res = db().table(table).insert(row).execute()
    return (res.data or [None])[0]


def update(table, values, **eq):
    return _filter(db().table(table).update(values), eq).execute().data or []


def delete(table, **eq):
    return _filter(db().table(table).delete(), eq).execute().data or []


def upsert(table, row, on_conflict=None):
    q = db().table(table).upsert(row, on_conflict=on_conflict) if on_conflict else db().table(table).upsert(row)
    return (q.execute().data or [None])[0]


def count(table, **eq):
    return _filter(db().table(table).select("*", count="exact"), eq).limit(1).execute().count or 0


def in_(table, col, values, cols="*", chunk=80):
    """`col IN (values)`. Long lists are split into several requests so the URL never exceeds proxy limits."""
    values = list({v for v in values if v is not None})
    out = []
    for i in range(0, len(values), chunk):
        out += db().table(table).select(cols).in_(col, values[i:i + chunk]).execute().data or []
    return out


def by_id(table, ids, cols="*"):
    return {r["id"]: r for r in in_(table, "id", ids, cols)}
