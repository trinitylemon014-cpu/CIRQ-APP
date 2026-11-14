"""Database-level tests of the CIRQ LIVE money, battle and guest logic, run against REAL PostgreSQL (not the fake DB).

They need the `psql` client and a server you can create databases on, e.g.:
    CIRQ_TEST_PG="-h localhost -p 5432 -U postgres" pytest tests/test_live_sql.py
Without CIRQ_TEST_PG the whole module is skipped. A throw-away database is created and dropped; Supabase's `auth`
schema and roles are stubbed by tests/sql/supabase_stub.sql. Concurrency tests open genuinely parallel connections.
"""
import json
import os
import shlex
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

PG = os.environ.get("CIRQ_TEST_PG")
pytestmark = pytest.mark.skipif(not PG, reason="set CIRQ_TEST_PG to run the real-PostgreSQL tests")
ROOT = os.path.join(os.path.dirname(__file__), "..")


def _psql(db, sql, check=True):
    cmd = ["psql", *shlex.split(PG), "-d", db, "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1", "-c", sql]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode:
        raise AssertionError(r.stderr)
    return r.stdout.strip()


def _file(db, path):
    cmd = ["psql", *shlex.split(PG), "-d", db, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.fixture(scope="module")
def dbname():
    name = "cirq_live_t_" + uuid.uuid4().hex[:8]
    _psql("postgres", f"create database {name}")
    try:
        for f in ("tests/sql/supabase_stub.sql", "schema.sql", "migrations/2026_11_14_live_streaming.sql"):
            _file(name, os.path.join(ROOT, f))
        yield name
    finally:
        _psql("postgres", f"drop database if exists {name} with (force)", check=False)


def user(db, name, coins=0):
    uid = str(uuid.uuid4())
    _psql(db, f"insert into auth.users(id,email) values ('{uid}','{name}-{uid[:6]}@x.test');"
              f"insert into profiles(id,username,display_name,account_status,coin_balance) values ('{uid}','{name}{uid[:6]}','{name}','approved',{coins})")
    return uid


def go_live(db, host, **kw):
    sid = str(uuid.uuid4())
    _psql(db, f"insert into live_sessions(id,host_id,title,room_name) values ('{sid}','{host}','Test live','room-{sid}')")
    return sid


def balance(db, uid):
    return int(_psql(db, f"select coin_balance from profiles where id='{uid}'"))


def gift(db, sender, session, code, req=None, cap=0):
    req = req or ("req-" + uuid.uuid4().hex)
    return json.loads(_psql(db, f"select cirq_live_send_gift('{sender}','{session}','{code}','{req}',{cap})::text"))


PRICES = [("red_rose", 10, 5), ("love_heart", 50, 25), ("mystery_box", 100, 50), ("golden_crown", 500, 250),
          ("treasure_chest", 1000, 500), ("sports_car", 2000, 1000), ("diamond_ring", 5000, 2500), ("private_jet", 10000, 5000)]


@pytest.mark.parametrize("code,price,host", PRICES)
def test_every_gift_charges_price_and_credits_exactly_half(dbname, code, price, host):
    h, v = user(dbname, "h"), user(dbname, "v", coins=20000)
    s = go_live(dbname, h)
    r = gift(dbname, v, s, code)
    assert r["status"] == "ok" and r["gross"] == price and r["host_coins"] == host and r["platform_coins"] == price - host
    assert balance(dbname, v) == 20000 - price and balance(dbname, h) == host
    assert r["balance"] == 20000 - price
    led = _psql(dbname, f"select user_id='{v}', amount, reason from coin_ledger where ref='{r['txn_id']}' order by amount")
    assert led.splitlines() == [f"t|{-price}|live_gift_out", f"f|{host}|live_gift_in"]
    assert int(_psql(dbname, f"select coins from live_creator_earnings where transaction_id='{r['txn_id']}'")) == host
    # the host's wallet equals the sum of its ledger: nothing is created or lost outside the ledger
    assert int(_psql(dbname, f"select coalesce(sum(amount),0) from coin_ledger where user_id='{h}'")) == balance(dbname, h)


def test_mystery_box_is_always_100_in_and_50_out(dbname):
    h, v = user(dbname, "h"), user(dbname, "v", coins=1000)
    s = go_live(dbname, h)
    for _ in range(5):
        r = gift(dbname, v, s, "mystery_box")
        assert (r["gross"], r["host_coins"], r["diamonds"]) == (100, 50, 0)
    assert balance(dbname, v) == 500 and balance(dbname, h) == 250


def test_treasure_chest_tracks_ten_diamonds_separately_from_500_earned(dbname):
    h, v = user(dbname, "h"), user(dbname, "v", coins=1000)
    s = go_live(dbname, h)
    r = gift(dbname, v, s, "treasure_chest")
    assert (r["gross"], r["host_coins"], r["diamonds"], r["diamond_value"]) == (1000, 500, 10, 100)
    assert balance(dbname, h) == 500                                   # NOT 500 + 1000 displayed
    n, shown, counted = _psql(dbname, f"select count(*), sum(display_value), count(*) filter (where counts_toward_earnings) from live_diamond_records where transaction_id='{r['txn_id']}'").split("|")
    assert (int(n), int(shown), int(counted)) == (10, 1000, 0)
    assert int(_psql(dbname, f"select sum(coins) from live_creator_earnings where host_id='{h}'")) == 500
    assert int(_psql(dbname, f"select diamonds_displayed from live_sessions where id='{s}'")) == 10
    summary = json.loads(_psql(dbname, f"select cirq_live_creator_summary('{h}')::text"))
    assert summary["host_earnings"] == 500 and summary["gross_value"] == 1000 and summary["diamonds_displayed"] == 10
    with pytest.raises(AssertionError):                                # diamonds can never be flagged as earnings
        _psql(dbname, f"update live_diamond_records set counts_toward_earnings = true where transaction_id='{r['txn_id']}'")


def test_insufficient_balance_changes_nothing(dbname):
    h, v = user(dbname, "h"), user(dbname, "v", coins=99)
    s = go_live(dbname, h)
    r = gift(dbname, v, s, "mystery_box")
    assert r["status"] == "insufficient" and r["price"] == 100
    assert balance(dbname, v) == 99 and balance(dbname, h) == 0
    assert int(_psql(dbname, f"select count(*) from live_gift_transactions where sender_id='{v}'")) == 0


def test_replayed_request_is_not_charged_twice(dbname):
    h, v = user(dbname, "h"), user(dbname, "v", coins=1000)
    s = go_live(dbname, h)
    first = gift(dbname, v, s, "golden_crown", req="same-request-1")
    again = gift(dbname, v, s, "golden_crown", req="same-request-1")
    assert first["status"] == "ok" and again["status"] == "duplicate" and again["txn_id"] == first["txn_id"]
    assert balance(dbname, v) == 500 and balance(dbname, h) == 250
    assert gift(dbname, v, s, "red_rose", req="same-request-1")["status"] == "request_reuse"
    assert balance(dbname, v) == 500


def test_self_gift_ended_session_unknown_gift_and_bad_request(dbname):
    h, v = user(dbname, "h", coins=500), user(dbname, "v", coins=500)
    s = go_live(dbname, h)
    assert gift(dbname, h, s, "red_rose")["status"] == "self_gift"
    assert gift(dbname, v, s, "free_gift")["status"] == "gift_unavailable"
    assert gift(dbname, v, s, "red_rose", req="x")["status"] == "bad_request"
    assert _psql(dbname, f"select cirq_live_end_session('{s}')") == "t"
    assert _psql(dbname, f"select cirq_live_end_session('{s}')") == "f"        # idempotent
    assert gift(dbname, v, s, "red_rose")["status"] == "session_ended"
    assert balance(dbname, v) == 500 and balance(dbname, h) == 500


def test_blocked_and_removed_viewers_cannot_gift(dbname):
    h, v1, v2 = user(dbname, "h"), user(dbname, "v1", coins=100), user(dbname, "v2", coins=100)
    s = go_live(dbname, h)
    _psql(dbname, f"insert into live_host_blocks(host_id,user_id) values ('{h}','{v1}')")
    _psql(dbname, f"insert into live_viewers(session_id,user_id,removed) values ('{s}','{v2}',true)")
    assert gift(dbname, v1, s, "red_rose")["status"] == "blocked"
    assert gift(dbname, v2, s, "red_rose")["status"] == "blocked"
    assert balance(dbname, v1) == 100 and balance(dbname, v2) == 100


def test_daily_cap(dbname):
    h, v = user(dbname, "h"), user(dbname, "v", coins=5000)
    s = go_live(dbname, h)
    assert gift(dbname, v, s, "golden_crown", cap=600)["status"] == "ok"
    r = gift(dbname, v, s, "mystery_box", cap=600)
    assert r["status"] == "ok"
    assert gift(dbname, v, s, "red_rose", cap=600)["status"] == "cap_reached"


def test_parallel_purchases_never_overdraw_or_lose_coins(dbname):
    h, v = user(dbname, "h"), user(dbname, "v", coins=1000)
    s = go_live(dbname, h)
    with ThreadPoolExecutor(20) as pool:
        results = list(pool.map(lambda _: gift(dbname, v, s, "mystery_box")["status"], range(20)))
    assert results.count("ok") == 10 and results.count("insufficient") == 10
    assert balance(dbname, v) == 0 and balance(dbname, h) == 500
    assert int(_psql(dbname, f"select count(*) from live_gift_transactions where sender_id='{v}'")) == 10


def test_parallel_double_submit_of_one_request_charges_once(dbname):
    h, v = user(dbname, "h"), user(dbname, "v", coins=1000)
    s = go_live(dbname, h)
    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(lambda _: gift(dbname, v, s, "golden_crown", req="double-tap-0001")["status"], range(12)))
    assert results.count("ok") == 1 and results.count("duplicate") == 11
    assert balance(dbname, v) == 500 and balance(dbname, h) == 250
    assert int(_psql(dbname, f"select count(*) from live_creator_earnings where host_id='{h}'")) == 1


def test_many_senders_to_one_host_totals_add_up(dbname):
    h = user(dbname, "h")
    s = go_live(dbname, h)
    senders = [user(dbname, f"v{i}", coins=200) for i in range(8)]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda v: [gift(dbname, v, s, "mystery_box") for _ in range(2)], senders))
    assert balance(dbname, h) == 8 * 2 * 50
    row = _psql(dbname, f"select gifts_count, gift_gross, host_earned, platform_share from live_sessions where id='{s}'")
    assert row == "16|1600|800|800"
    assert int(_psql(dbname, f"select sum(coins) from live_creator_earnings where session_id='{s}'")) == 800


def test_supporters_and_fan_club_are_recorded(dbname):
    h, v = user(dbname, "h"), user(dbname, "v", coins=1000)
    s = go_live(dbname, h)
    gift(dbname, v, s, "golden_crown"); gift(dbname, v, s, "red_rose")
    assert _psql(dbname, f"select total_coins, gifts_count from live_supporters where host_id='{h}' and user_id='{v}'") == "510|2"
    assert _psql(dbname, f"select count(*) from live_fan_club_members where host_id='{h}' and user_id='{v}'") == "1"


def test_one_live_session_per_host(dbname):
    h = user(dbname, "h")
    go_live(dbname, h)
    with pytest.raises(AssertionError):
        go_live(dbname, h)


# ------------------------------------------------------------------ PK battles
def make_battle(db, ha, hb, sa, sb, duration=60, countdown=0, accept=True):
    bid = str(uuid.uuid4())
    _psql(db, f"insert into live_battles(id,session_a,session_b,host_a,host_b,duration_seconds,expires_at) values "
              f"('{bid}','{sa}','{sb}','{ha}','{hb}',{duration}, now() + interval '1 minute');"
              f"insert into live_battle_participants(battle_id,host_id,session_id,side) values ('{bid}','{ha}','{sa}','a'),('{bid}','{hb}','{sb}','b')")
    return bid, (_psql(db, f"select cirq_live_battle_accept('{bid}','{hb}',{countdown})") if accept else None)


def scores(db, bid):
    return _psql(db, f"select string_agg(side||':'||score, ',' order by side) from live_battle_participants where battle_id='{bid}'")


def test_battle_scoring_winner_and_no_extra_coins(dbname):
    ha, hb, v = user(dbname, "ha"), user(dbname, "hb"), user(dbname, "v", coins=5000)
    sa, sb = go_live(dbname, ha), go_live(dbname, hb)
    bid, res = make_battle(dbname, ha, hb, sa, sb, countdown=0)
    assert res == "ok"
    assert _psql(dbname, f"select cirq_live_battle_tick('{bid}')") == "active"
    assert gift(dbname, v, sa, "golden_crown")["scores"] == {"a": 500, "b": 0}
    gift(dbname, v, sb, "treasure_chest"); gift(dbname, v, sa, "mystery_box")
    assert scores(dbname, bid) == "a:600,b:1000"
    # every counted gift has the battle id; score == sum of those gifts' gross value (no duplicate/missed updates)
    assert int(_psql(dbname, f"select sum(gross_coins) from live_gift_transactions where battle_id='{bid}' and host_id='{ha}'")) == 600
    before = (balance(dbname, ha), balance(dbname, hb))
    assert _psql(dbname, f"select cirq_live_battle_tick('{bid}', true)") == "ended"
    assert _psql(dbname, f"select outcome, winner_host_id='{hb}' from live_battles where id='{bid}'") == "b|t"
    assert (balance(dbname, ha), balance(dbname, hb)) == before                 # winning pays nothing extra
    assert _psql(dbname, f"select cirq_live_battle_tick('{bid}', true)") == "ended"   # idempotent: no second result event
    assert int(_psql(dbname, f"select count(*) from live_events where kind='battle_end' and session_id='{sa}'")) == 1
    gift(dbname, v, sa, "red_rose")                                             # after the battle: not scored
    assert scores(dbname, bid) == "a:600,b:1000"


def test_battle_draw_and_countdown_gifts_not_counted(dbname):
    ha, hb, v = user(dbname, "ha"), user(dbname, "hb"), user(dbname, "v", coins=5000)
    sa, sb = go_live(dbname, ha), go_live(dbname, hb)
    bid, res = make_battle(dbname, ha, hb, sa, sb, countdown=30)
    assert res == "ok" and _psql(dbname, f"select cirq_live_battle_tick('{bid}')") == "countdown"
    r = gift(dbname, v, sa, "golden_crown")
    assert r["battle_id"] is None and scores(dbname, bid) == "a:0,b:0"          # not started yet
    _psql(dbname, f"update live_battles set starts_at = now() - interval '1 second' where id='{bid}'")
    gift(dbname, v, sa, "mystery_box"); gift(dbname, v, sb, "mystery_box")
    _psql(dbname, f"update live_battles set ends_at = now() - interval '1 second' where id='{bid}'")
    assert _psql(dbname, f"select cirq_live_battle_tick('{bid}')") == "ended"
    assert _psql(dbname, f"select outcome, winner_host_id is null from live_battles where id='{bid}'") == "draw|t"


def test_invalid_battle_states(dbname):
    ha, hb, hc = user(dbname, "ha"), user(dbname, "hb"), user(dbname, "hc")
    sa, sb, sc = go_live(dbname, ha), go_live(dbname, hb), go_live(dbname, hc)
    b1, r1 = make_battle(dbname, ha, hb, sa, sb, countdown=10)
    assert r1 == "ok"
    assert _psql(dbname, f"select cirq_live_battle_accept('{b1}','{hb}')") == "bad_state"       # cannot accept twice
    b2, r2 = make_battle(dbname, hc, hb, sc, sb, accept=False)
    assert _psql(dbname, f"select cirq_live_battle_accept('{b2}','{hb}')") == "busy"            # hb is already fighting
    b3, _ = make_battle(dbname, hc, ha, sc, sa, accept=False)
    assert _psql(dbname, f"select cirq_live_battle_accept('{b3}','{hc}')") == "forbidden"       # only the invited host accepts
    assert _psql(dbname, f"select active from live_battle_participants where battle_id='{b2}' and host_id='{hb}'") == "f"
    with pytest.raises(AssertionError):                                                         # a host cannot fight themselves
        _psql(dbname, f"insert into live_battles(session_a,session_b,host_a,host_b,duration_seconds,expires_at) values ('{sa}','{sa}','{ha}','{ha}',60,now())")
    # ending a session ends its battle too
    _psql(dbname, f"select cirq_live_end_session('{sa}')")
    assert _psql(dbname, f"select status from live_battles where id='{b1}'") == "cancelled"
    assert _psql(dbname, f"select count(*) from live_battle_participants where active and host_id in ('{ha}','{hb}')") == "0"


def test_expired_invite_cannot_be_accepted(dbname):
    ha, hb = user(dbname, "ha"), user(dbname, "hb")
    sa, sb = go_live(dbname, ha), go_live(dbname, hb)
    bid, _ = make_battle(dbname, ha, hb, sa, sb, accept=False)
    _psql(dbname, f"update live_battles set expires_at = now() - interval '1 second' where id='{bid}'")
    assert _psql(dbname, f"select cirq_live_battle_accept('{bid}','{hb}')") == "expired"


# ------------------------------------------------------------------ guests
def test_guest_limit_holds_under_parallel_acceptance(dbname):
    h = user(dbname, "h")
    s = go_live(dbname, h)
    invites = []
    for i in range(7):
        g = user(dbname, f"g{i}")
        iid = str(uuid.uuid4())
        _psql(dbname, f"insert into live_guest_invites(id,session_id,guest_id,initiated_by) values ('{iid}','{s}','{g}','host')")
        invites.append((iid, g))
    with ThreadPoolExecutor(7) as pool:
        out = list(pool.map(lambda x: _psql(dbname, f"select cirq_live_guest_accept('{x[0]}','{x[1]}',4)"), invites))
    assert out.count("ok") == 4 and out.count("full") == 3
    assert int(_psql(dbname, f"select count(*) from live_guest_invites where session_id='{s}' and status='accepted' and consent_at is not null")) == 4


def test_guest_acceptance_rules_and_consent(dbname):
    h, g, other = user(dbname, "h"), user(dbname, "g"), user(dbname, "o")
    s = go_live(dbname, h)
    inv, req = str(uuid.uuid4()), str(uuid.uuid4())
    _psql(dbname, f"insert into live_guest_invites(id,session_id,guest_id,initiated_by) values ('{inv}','{s}','{g}','host')")
    assert _psql(dbname, f"select cirq_live_guest_accept('{inv}','{h}')") == "forbidden"        # host cannot accept on the guest's behalf
    assert _psql(dbname, f"select cirq_live_guest_accept('{inv}','{other}')") == "forbidden"
    assert _psql(dbname, f"select consent_at is null from live_guest_invites where id='{inv}'") == "t"
    assert _psql(dbname, f"select cirq_live_guest_accept('{inv}','{g}')") == "ok"
    assert _psql(dbname, f"select consent_at is not null from live_guest_invites where id='{inv}'") == "t"
    _psql(dbname, f"insert into live_guest_invites(id,session_id,guest_id,initiated_by,status,consent_at) values ('{req}','{s}','{other}','guest','requested',now())")
    assert _psql(dbname, f"select cirq_live_guest_accept('{req}','{other}')") == "forbidden"    # a requester cannot approve themselves
    assert _psql(dbname, f"select cirq_live_guest_accept('{req}','{h}')") == "ok"
    with pytest.raises(AssertionError):                                                          # no accepted guest without consent
        _psql(dbname, f"update live_guest_invites set status='accepted', consent_at=null where id='{inv}'")


def test_ending_session_releases_guests_and_viewers(dbname):
    h, g, v = user(dbname, "h"), user(dbname, "g"), user(dbname, "v")
    s = go_live(dbname, h)
    _psql(dbname, f"insert into live_guest_invites(session_id,guest_id,initiated_by,status,consent_at) values ('{s}','{g}','host','accepted',now())")
    _psql(dbname, f"insert into live_viewers(session_id,user_id) values ('{s}','{v}')")
    _psql(dbname, f"select cirq_live_end_session('{s}','host_ended')")
    assert _psql(dbname, f"select status from live_guest_invites where session_id='{s}'") == "left"
    assert _psql(dbname, f"select left_at is not null from live_viewers where session_id='{s}'") == "t"
    assert _psql(dbname, f"select kind from live_events where session_id='{s}' order by id desc limit 1") == "ended"


def test_rankings_and_existing_ledger_untouched(dbname):
    h1, h2, v = user(dbname, "h1"), user(dbname, "h2"), user(dbname, "v", coins=10000)
    s1, s2 = go_live(dbname, h1), go_live(dbname, h2)
    gift(dbname, v, s1, "golden_crown"); gift(dbname, v, s2, "red_rose")
    top = _psql(dbname, "select host_id, host_coins from cirq_live_host_ranking(null, 100)").splitlines()
    assert f"{h1}|250" in top and f"{h2}|5" in top and top.index(f"{h1}|250") < top.index(f"{h2}|5")
    assert _psql(dbname, f"select user_id, total_coins from cirq_live_top_gifters('{h1}', null, 5)") == f"{v}|500"
    # the migration never edits old rows: every ledger row it created uses its own reasons
    assert _psql(dbname, "select count(*) from coin_ledger where reason not in ('live_gift_in','live_gift_out')") == "0"
