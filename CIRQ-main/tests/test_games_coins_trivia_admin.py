import io
import pytest
from datetime import timedelta
from tests.conftest import login
from app.utils import tier_for, TIERS, utcnow, iso


# ---------------- CIRQ STATUS tiers
@pytest.mark.parametrize("n,name", [(0, "Rookie"), (99, "Rookie"), (100, "Elite"), (500, "Elite"), (501, "Legendary"), (1000, "Legendary"),
                                    (1001, "Icon"), (2500, "Icon"), (2501, "Superstar"), (5000, "Superstar"), (5001, "Royalty"),
                                    (10000, "Royalty"), (10001, "Hall of Fame"), (999999, "Hall of Fame")])
def test_tier_boundaries(n, name):
    assert tier_for(n)["name"] == name


def test_tiers_contiguous_and_non_overlapping():
    for (_, _, hi), (_, lo, _) in zip(TIERS, TIERS[1:]):
        assert lo == hi + 1
    assert TIERS[0][1] == 0 and TIERS[-1][2] is None


def test_rank_comes_from_real_follows_not_editable_fields(as_user, db):
    a, pa = as_user("alice"); b, _ = as_user("bob")
    b.post("/u/alice/follow")
    assert b"1 follower" in a.get("/status").data
    a.post("/settings", data={"action": "profile", "display_name": "A", "follower_count": "100000"})
    assert b"Rookie" in a.get("/status").data


# ---------------- CIRQ FLIP (deck of real students, hidden developer)
@pytest.fixture
def table(as_user, mk):
    """A logged-in player plus enough real students and the developer account to deal a deck."""
    c, me = as_user("alice")
    for i in range(7):
        mk(f"student{i}")
    mk("vex", role="admin")
    return c, me


def start(c):
    return c.post("/api/games/flip/start").get_json()


def guess(c, i):
    return c.post("/api/games/flip/guess", json={"card": i})


def rnd(db):
    return db.data["game_rounds"][-1]


def answer_index(db):
    return rnd(db)["answer_index"]


def test_not_enough_students_means_no_game_and_no_fake_users(as_user, db):
    c, _ = as_user("alice")
    r = c.post("/api/games/flip/start")
    assert r.status_code == 409 and not db.data.get("game_rounds")
    assert len(db.data["profiles"]) == 1                                 # nothing was invented


def test_deck_is_real_students_with_one_developer(table, db):
    c, me = table
    s = start(c)
    deck = rnd(db)["deck"]
    assert s["cards"] == len(deck) == 8 and sum(e["developer"] for e in deck) == 1
    ids = {p["id"]: p for p in db.data["profiles"]}
    vex = next(p for p in db.data["profiles"] if p["username"] == "vex")
    assert deck[answer_index(db)]["user_id"] == vex["id"] and deck[answer_index(db)]["developer"]
    others = [e["user_id"] for e in deck if not e["developer"]]
    assert len(set(others)) == len(others) and me["id"] not in others and all(ids[o]["account_status"] == "approved" for o in others)


def test_private_blocked_and_unapproved_users_never_in_deck(as_user, mk, db):
    c, me = as_user("alice")
    for i in range(6): mk(f"s{i}")
    priv = mk("secretive"); db.data["profiles"][[p["id"] for p in db.data["profiles"]].index(priv["id"])]["is_private"] = True
    pend = mk("pendingkid", status="pending"); blocked = mk("blockedkid"); db.table("blocks").insert({"blocker_id": me["id"], "blocked_id": blocked["id"]}).execute()
    mk("vex", role="admin")
    start(c)
    ids = {e["user_id"] for e in rnd(db)["deck"]}
    assert not ids & {priv["id"], pend["id"], blocked["id"]}


def test_answer_never_leaks_while_round_active(table, db):
    c, _ = table
    s = start(c)
    assert set(s) == {"round_id", "status", "cards", "guesses_used", "guesses_left", "flipped"}
    ai = answer_index(db)
    api_dump = c.post("/api/games/flip/start").get_data(as_text=True) + c.get("/api/games/flip/state").get_data(as_text=True)
    page = c.get("/games/flip").get_data(as_text=True)
    assert "answer_index" not in api_dump + page and '"deck"' not in api_dump and "user_id" not in api_dump + page
    assert str(ai) not in c.post("/api/games/flip/start").get_json().get("flipped", [])
    wrong = (ai + 1) % s["cards"]
    r = guess(c, wrong).get_json()
    assert r["status"] == "active" and "answer_index" not in r and "developer" not in r
    assert r["flipped"][0]["profile"]["developer"] is False and r["flipped"][0]["correct"] is False


def test_flipped_card_shows_real_profile_data_only(table, db):
    c, _ = table; s = start(c); ai = answer_index(db)
    card = guess(c, (ai + 1) % s["cards"]).get_json()["flipped"][0]["profile"]
    real = {p["username"]: p for p in db.data["profiles"]}
    assert card["username"] in real and card["name"] == real[card["username"]]["display_name"]
    assert set(card) == {"developer", "name", "username", "school", "avatar", "followers", "tier", "tier_index"}   # no ids or emails


def test_five_guess_limit_and_no_repeats_and_validation(table, db):
    c, _ = table; s = start(c); ai = answer_index(db); wrongs = [i for i in range(s["cards"]) if i != ai]
    assert guess(c, wrongs[0]).status_code == 200
    assert guess(c, wrongs[0]).status_code == 409                       # repeat
    for bad in (-1, s["cards"], "3", None, True, 1.5):
        assert guess(c, bad).status_code == 400
    for w in wrongs[1:4]:
        assert guess(c, w).status_code == 200
    last = guess(c, wrongs[4]).get_json()
    assert last["status"] == "lost" and last["answer_index"] == ai and last["guesses_left"] == 0 and last["developer"]["name"] == "VEX"
    assert guess(c, wrongs[5]).status_code == 409                       # round over
    assert len(db.data["game_guesses"]) == 5


def test_answer_and_deck_fixed_for_the_whole_round(table, db):
    c, _ = table; start(c); ai, deck = answer_index(db), rnd(db)["deck"]
    start(c); start(c)
    assert answer_index(db) == ai and rnd(db)["deck"] == deck and len(db.data["game_rounds"]) == 1


def test_winning_awards_500_coins_once_and_achievements(table, db):
    c, p = table; start(c); ai = answer_index(db)
    r = guess(c, ai).get_json()
    assert r["status"] == "won" and r["reward"]["coins"] == 500 and r["developer"]["name"] == "VEX" and r["developer"]["developer"] is True
    assert next(x for x in db.data["profiles"] if x["id"] == p["id"])["coin_balance"] == 500
    assert {"dev_finder", "flip_first_win"} <= {a["code"] for a in db.data["user_achievements"]}
    assert guess(c, (ai + 1) % r["cards"]).status_code == 409           # no guesses after a win
    assert next(x for x in db.data["profiles"] if x["id"] == p["id"])["coin_balance"] == 500


def test_daily_reward_cap(table, db, app):
    c, p = table; app.config["DEV_CARD_REWARDS_PER_DAY"] = 2
    results = []
    for _ in range(3):
        start(c); results.append(guess(c, answer_index(db)).get_json()["reward"]["coins"])
    assert results == [500, 500, 0]
    assert next(x for x in db.data["profiles"] if x["id"] == p["id"])["coin_balance"] == 1000


def test_default_daily_cap_is_one(table, db):
    c, _ = table
    got = []
    for _ in range(2):
        start(c); got.append(guess(c, answer_index(db)).get_json()["reward"]["coins"])
    assert got == [500, 0]


def test_developer_card_links_to_developer_profile(table, db):
    c, _ = table; start(c)
    assert guess(c, answer_index(db)).get_json()["developer"]["username"] == "vex"


def test_game_works_with_virtual_developer_when_account_missing(as_user, mk, db):
    c, _ = as_user("alice")
    for i in range(6): mk(f"s{i}")
    s = start(c); r = guess(c, answer_index(db)).get_json()
    assert r["status"] == "won" and r["developer"]["name"] == "VEX" and r["developer"]["username"] is None


def test_flip_page_renders_without_revealing_cards(table):
    c, _ = table
    html = c.get("/games/flip").get_data(as_text=True)
    assert "Guess the" in html and "500" in html and "student" not in html.split('id="flip-deck"')[1].split("</div>")[0]


def test_games_free_and_paid_flag_enforced_on_server(as_user, app):
    c, _ = as_user("alice")
    assert c.post("/games/flip/paid").status_code == 403                # disabled by default
    assert app.config["PAID_COMPETITIONS_ENABLED"] is False
    app.config["PAID_COMPETITIONS_ENABLED"] = True
    assert c.post("/games/flip/paid").status_code == 501                # even when on, no paid FLIP exists
    assert c.get("/games/flip").status_code == 200


# ---------------- coins & boosts
def give(db, uid, n):
    db.data["profiles"][[p["id"] for p in db.data["profiles"]].index(uid)]["coin_balance"] += n


def test_boost_with_coins(as_user, db):
    c, p = as_user("alice")
    r = c.post("/coins/boost", follow_redirects=True)
    assert b"need 1000 coins" in r.data and not db.data.get("boosts")
    give(db, p["id"], 1000)
    c.post("/coins/boost")
    prof = db.data["profiles"][0]
    assert prof["coin_balance"] == 0 and db.data["boosts"][0]["source"] == "coins"
    assert (utcnow() + timedelta(days=2, hours=23)).isoformat() < prof["boosted_until"] < (utcnow() + timedelta(days=3, minutes=1)).isoformat()
    assert b"Boosted" in c.get("/coins").data
    assert any(a["code"] == "booster" for a in db.data["user_achievements"])


def test_coin_balance_cannot_go_negative_or_be_double_granted(app, db, mk):
    from app.services import coin_service
    p = mk("alice")
    with app.app_context():
        assert coin_service.apply(p["id"], -5, "boost_spend", "x") == coin_service.INSUFFICIENT
        assert coin_service.apply(p["id"], 10, "welcome", "w") == 10
        assert coin_service.apply(p["id"], 10, "welcome", "w") == coin_service.DUPLICATE
    assert db.data["profiles"][0]["coin_balance"] == 10


def test_boosted_users_listed_first_and_labelled_in_discover(as_user, db, mk):
    c, _ = as_user("alice"); mk("zed"); b = mk("boosty")
    db.data["profiles"][[p["id"] for p in db.data["profiles"]].index(b["id"])]["boosted_until"] = iso(utcnow() + timedelta(days=1))
    page = c.get("/discover?tab=people").get_data(as_text=True)
    assert page.index("@boosty") < page.index("@zed") and "Boosted" in page
    assert tier_for(0)["name"] == "Rookie"                              # boosting does not change rank


# ---------------- manual payments
def enable_paid(app): app.config["PAID_COMPETITIONS_ENABLED"] = True


def add_number(c, phone="0772123456", name="NAKATO JANE", consent="on"):
    return c.post("/pay/number", data={"phone": phone, "account_name": name, "guardian_confirmed": consent, "network": "mtn"})


def adm(app, mk):
    mk("boss", role="admin"); c = app.test_client(); login(c, "boss"); return c


def test_payment_page_and_registration_disabled_by_default(as_user, db):
    c, _ = as_user("alice")
    assert b"open yet" in c.get("/pay").data
    assert add_number(c).status_code == 403 and not db.data.get("payer_numbers")


def test_register_number_validation_and_consent(app, as_user, db):
    enable_paid(app); c, _ = as_user("alice")
    add_number(c, phone="12345"); add_number(c, consent="")
    assert not db.data.get("payer_numbers")
    add_number(c)
    n = db.data["payer_numbers"][0]
    assert n["phone"] == "+256772123456" and n["status"] == "pending" and n["guardian_confirmed"]
    b, _ = as_user("bob"); add_number(b)                                  # same number can't be claimed twice
    assert len(db.data["payer_numbers"]) == 1


def test_numbers_are_private_to_admins(app, as_user, db):
    enable_paid(app); a, _ = as_user("alice"); add_number(a)
    b, _ = as_user("bob")
    assert b"772123456" not in b.get("/pay").data and b.get("/admin/payments").status_code == 403


def test_admin_verifies_number_then_records_payment_and_credits_entry(app, as_user, db, mk):
    enable_paid(app); a, pa = as_user("alice"); add_number(a)
    boss = adm(app, mk); nid = db.data["payer_numbers"][0]["id"]
    rec = lambda **kw: boss.post("/admin/payments/record", data={"payer_number_id": nid, "provider_reference": "TX100001", "purpose": "trivia_entry", "amount_ugx": "1000", **kw})
    rec(); assert not db.data.get("payments")                             # number not verified yet
    boss.post(f"/admin/payments/numbers/{nid}/approve")
    rec()
    assert len(db.data["payments"]) == 1 and len(db.data["trivia_entry_credits"]) == 1
    rec(); assert len(db.data["payments"]) == 1                           # same transaction ID can't credit twice
    rec(provider_reference="TX100002", amount_ugx="3000")
    assert len(db.data["trivia_entry_credits"]) == 4
    rec(provider_reference="TX100003", amount_ugx="500"); assert len(db.data["payments"]) == 2   # too little
    assert any(x["action"] == "payment_recorded" for x in db.data["audit_logs"])


def test_boost_purchases_3_and_5_days_with_exact_prices(app, as_user, db, mk):
    enable_paid(app); a, pa = as_user("alice"); add_number(a); boss = adm(app, mk)
    nid = db.data["payer_numbers"][0]["id"]; boss.post(f"/admin/payments/numbers/{nid}/approve")
    rec = lambda ref, purpose, amt: boss.post("/admin/payments/record", data={"payer_number_id": nid, "provider_reference": ref, "purpose": purpose, "amount_ugx": amt})
    rec("B1", "boost_3d", "999"); assert not db.data.get("boosts")        # wrong amount
    rec("B1234", "boost_3d", "1000")
    first_end = db.data["boosts"][0]["ends_at"]
    rec("B5678", "boost_5d", "7000")
    assert len(db.data["boosts"]) == 2 and db.data["boosts"][1]["ends_at"] > first_end      # stacks after the running boost
    days = (db.data["boosts"][1]["ends_at"] > (utcnow() + timedelta(days=7, hours=23)).isoformat())
    assert days


def test_student_cannot_self_credit(app, as_user, db):
    enable_paid(app); a, _ = as_user("alice"); add_number(a)
    nid = db.data["payer_numbers"][0]["id"]
    assert a.post("/admin/payments/record", data={"payer_number_id": nid, "provider_reference": "FREE1", "purpose": "trivia_entry", "amount_ugx": "1000"}).status_code == 403
    assert a.post(f"/admin/payments/numbers/{nid}/approve").status_code == 403


# ---------------- trivia
def make_set(boss, db, n=3, publish=True):
    boss.post("/admin/trivia", data={"title": "General knowledge", "description": "warm up"})
    sid = db.data["trivia_sets"][0]["id"]
    for i in range(n):
        boss.post(f"/admin/trivia/{sid}/questions", data={"prompt": f"Question {i + 1}?", "option0": "A1", "option1": "B1", "option2": "C1", "option3": "D1", "correct": "2", "time_limit_seconds": "20"})
    if publish: boss.post(f"/admin/trivia/{sid}/status/published")
    return sid


def test_admin_authors_and_publishes_trivia(app, as_user, db, mk):
    boss = adm(app, mk); sid = make_set(boss, db, 2, publish=False)
    assert len(db.data["trivia_questions"]) == 2 and db.data["trivia_sets"][0]["status"] == "draft"
    c, _ = as_user("alice")
    assert c.get(f"/trivia/{sid}").status_code == 404                    # drafts invisible
    boss.post(f"/admin/trivia/{sid}/questions", data={"prompt": "Dup?", "option0": "x", "option1": "x", "option2": "y", "option3": "z", "correct": "0"})
    assert len(db.data["trivia_questions"]) == 2                          # duplicate options rejected
    boss.post(f"/admin/trivia/{sid}/status/published")
    assert b"General knowledge" in c.get("/trivia").data


def test_admin_imports_csv_questions_and_existing_score_awards_coins(app, as_user, db, mk):
    boss = adm(app, mk)
    boss.post("/admin/trivia", data={"title": "World knowledge"})
    sid = db.data["trivia_sets"][0]["id"]
    csv_data = (
        "question,option_a,option_b,option_c,option_d,correct_answer,time_limit_seconds\n"
        "Capital of France?,Berlin,Paris,Rome,Madrid,B,20\n"
        "Largest planet?,Earth,Mars,Jupiter,Venus,Jupiter,15\n"
    ).encode()
    response = boss.post(f"/admin/trivia/{sid}/questions/import", data={"questions_csv": (io.BytesIO(csv_data), "questions.csv")},
                         content_type="multipart/form-data", follow_redirects=True)
    assert b"Imported 2 questions" in response.data
    questions = sorted(db.data["trivia_questions"], key=lambda q: q["position"])
    assert [q["correct_index"] for q in questions] == [1, 2]
    assert [q["options"][q["correct_index"]] for q in questions] == ["Paris", "Jupiter"]

    boss.post(f"/admin/trivia/{sid}/status/published")
    c, player = as_user("alice")
    result = play(c, sid, [1, 2]).get_json()
    attempt = db.data["trivia_attempts"][0]
    assert result["done"] and attempt["correct_count"] == 2 and attempt["coins_awarded"] > 0
    assert next(p for p in db.data["profiles"] if p["id"] == player["id"])["coin_balance"] == attempt["coins_awarded"]


def test_trivia_csv_import_rejects_bad_rows_without_partial_import(app, db, mk):
    boss = adm(app, mk)
    boss.post("/admin/trivia", data={"title": "Draft set"})
    sid = db.data["trivia_sets"][0]["id"]
    csv_data = (
        "question,option_a,option_b,option_c,option_d,correct_answer\n"
        "Valid question?,one,two,three,four,A\n"
        "Invalid answer?,one,two,three,four,Z\n"
    ).encode()
    response = boss.post(f"/admin/trivia/{sid}/questions/import", data={"questions_csv": (io.BytesIO(csv_data), "questions.csv")},
                         content_type="multipart/form-data", follow_redirects=True)
    assert b"Nothing was imported" in response.data
    assert not db.data.get("trivia_questions")


def test_trivia_csv_import_rejects_overlong_time_instead_of_truncating(app, db, mk):
    boss = adm(app, mk)
    boss.post("/admin/trivia", data={"title": "Draft set"})
    sid = db.data["trivia_sets"][0]["id"]
    csv_data = (
        "question,option_a,option_b,option_c,option_d,correct_answer,time_limit_seconds\n"
        "Valid question?,one,two,three,four,A,1000\n"
    ).encode()
    response = boss.post(f"/admin/trivia/{sid}/questions/import", data={"questions_csv": (io.BytesIO(csv_data), "questions.csv")},
                         content_type="multipart/form-data", follow_redirects=True)
    assert b"Nothing was imported" in response.data
    assert not db.data.get("trivia_questions")


def test_admin_imports_text_pdf_questions(app, db, mk, monkeypatch):
    from app.blueprints.admin import routes as admin_routes

    class Page:
        def extract_text(self):
            return """Trivia
Question 1: Capital of France?
A. Berlin
B. Paris
C. Rome
D. Madrid
Correct answer: B
Time limit: 25 seconds
"""

    class Reader:
        is_encrypted = False
        pages = [Page()]

        def __init__(self, _file, strict=True):
            pass

    monkeypatch.setattr(admin_routes, "PdfReader", Reader)
    boss = adm(app, mk)
    boss.post("/admin/trivia", data={"title": "PDF set"})
    sid = db.data["trivia_sets"][0]["id"]
    response = boss.post(f"/admin/trivia/{sid}/questions/import", data={"questions_file": (io.BytesIO(b"%PDF-test"), "questions.pdf")},
                         content_type="multipart/form-data", follow_redirects=True)
    assert b"Imported 1 questions" in response.data
    question = db.data["trivia_questions"][0]
    assert question["prompt"] == "Capital of France?"
    assert question["options"][question["correct_index"]] == "Paris"
    assert question["time_limit_seconds"] == 25


def test_admin_pdf_import_rejects_image_only_document(app, db, mk, monkeypatch):
    from app.blueprints.admin import routes as admin_routes

    class Page:
        def extract_text(self):
            return ""

    class Reader:
        is_encrypted = False
        pages = [Page()]

        def __init__(self, _file, strict=True):
            pass

    monkeypatch.setattr(admin_routes, "PdfReader", Reader)
    boss = adm(app, mk)
    boss.post("/admin/trivia", data={"title": "PDF set"})
    sid = db.data["trivia_sets"][0]["id"]
    response = boss.post(f"/admin/trivia/{sid}/questions/import", data={"questions_file": (io.BytesIO(b"%PDF-test"), "scan.pdf")},
                         content_type="multipart/form-data", follow_redirects=True)
    assert b"Scanned PDFs aren't supported" in response.data
    assert not db.data.get("trivia_questions")


def test_non_admin_cannot_author_trivia(as_user, db):
    c, _ = as_user("alice")
    assert c.post("/admin/trivia", data={"title": "Hax"}).status_code == 403 and not db.data.get("trivia_sets")


def play(c, sid, choices, pause=0):
    c.post(f"/trivia/{sid}/start")
    out = None
    for i, ch in enumerate(choices, start=1):
        q = c.get(f"/api/trivia/{sid}/question").get_json()
        assert "correct_index" not in str(q) and q["position"] == i
        out = c.post(f"/api/trivia/{sid}/answer", json={"choice": ch, "position": i})
    return out


def test_free_trivia_awards_real_coins_once(app, as_user, db, mk):
    boss = adm(app, mk); sid = make_set(boss, db, 3)
    c, me = as_user("alice")
    out = play(c, sid, [2, 2, 2]).get_json()
    att = db.data["trivia_attempts"][0]
    assert out["done"] and att["paid"] is False and att["correct_count"] == 3
    # coins = points / TRIVIA_POINTS_PER_COIN (capped), credited immediately, even though the set was free
    expect = min(app.config["TRIVIA_MAX_COINS"], att["points"] // app.config["TRIVIA_POINTS_PER_COIN"])
    assert expect > 0 and att["coins_awarded"] == expect
    assert db.data["profiles"][-1]["coin_balance"] == expect
    assert [r["reason"] for r in db.data["coin_ledger"]] == ["trivia"]
    # finishing again (double submit / refresh) can never pay twice
    from app.blueprints.trivia.routes import _finish
    with app.test_request_context():
        _finish(att)
    assert db.data["profiles"][-1]["coin_balance"] == expect and len(db.data["coin_ledger"]) == 1
    assert f"+{expect} app coins earned" in c.get(f"/trivia/{sid}/results").get_data(as_text=True)


def test_paid_trivia_requires_entry_scores_accuracy_and_speed_and_awards_coins(app, as_user, db, mk):
    enable_paid(app); boss = adm(app, mk); sid = make_set(boss, db, 4)
    c, pa = as_user("alice")
    assert c.post(f"/trivia/{sid}/start").status_code == 302 and "/pay" in c.post(f"/trivia/{sid}/start").headers["Location"]
    assert not db.data.get("trivia_attempts")                              # no entry, no play
    db.table("trivia_entry_credits").insert({"user_id": pa["id"]}).execute()
    out = play(c, sid, [2, 2, 0, None])                                    # 2 right, 1 wrong, 1 skipped (timeout via null)
    assert out.get_json()["done"]
    att = db.data["trivia_attempts"][0]
    assert att["paid"] and att["correct_count"] == 2 and att["accuracy"] == 50.0
    assert 2 * 100 < att["points"] <= 2 * 200                              # base + speed bonus
    assert att["coins_awarded"] == att["points"] // 20
    assert [a["points"] for a in db.data["trivia_answers"]][2:] == [0, 0]
    assert db.data["trivia_entry_credits"][0]["used_at"]
    assert c.post(f"/trivia/{sid}/start").headers["Location"].endswith("/results")   # one attempt per set
    page = c.get(f"/trivia/{sid}/results").get_data(as_text=True)
    assert "Accuracy 50%" in page and "your answer" in page


def test_speed_bonus_scoring_and_server_side_timing(app, as_user, db, mk):
    from app.blueprints.trivia.routes import score_answer
    assert score_answer(True, 0, 20) == 200 and score_answer(True, 20000, 20) == 100 and score_answer(True, 10000, 20) == 150
    assert score_answer(False, 100, 20) == 0
    boss = adm(app, mk); sid = make_set(boss, db, 1); c, _ = as_user("alice")
    c.post(f"/trivia/{sid}/start")
    db.data["trivia_attempts"][0]["question_served_at"] = iso(utcnow() - timedelta(seconds=60))   # answered far too late
    r = c.post(f"/api/trivia/{sid}/answer", json={"choice": 2, "position": 1}).get_json()
    assert db.data["trivia_answers"][0]["correct"] is False and db.data["trivia_answers"][0]["points"] == 0


def test_trivia_double_submit_and_bad_input_rejected(app, as_user, db, mk):
    boss = adm(app, mk); sid = make_set(boss, db, 2); c, _ = as_user("alice")
    c.post(f"/trivia/{sid}/start")
    assert c.post(f"/api/trivia/{sid}/answer", json={"choice": 9, "position": 1}).status_code == 400
    assert c.post(f"/api/trivia/{sid}/answer", json={"choice": 2, "position": 1}).status_code == 200
    assert c.post(f"/api/trivia/{sid}/answer", json={"choice": 2, "position": 1}).status_code == 409
    assert len(db.data["trivia_answers"]) == 1


def test_trivia_answers_hidden_until_finished(app, as_user, db, mk):
    boss = adm(app, mk); sid = make_set(boss, db, 2); c, _ = as_user("alice")
    c.post(f"/trivia/{sid}/start")
    assert c.get(f"/trivia/{sid}/results").status_code == 302


# ---------------- admin protection
@pytest.mark.parametrize("path", ["/admin", "/admin/users", "/admin/schools", "/admin/password-resets", "/admin/reports", "/admin/media",
                                  "/admin/payments", "/admin/trivia", "/admin/flags", "/admin/audit"])
def test_admin_pages_blocked_for_students_and_anonymous(client, as_user, path):
    assert client.get(path).status_code == 302
    c, _ = as_user("alice")
    assert c.get(path).status_code == 403


def test_admin_pages_render_for_admin(app, mk, school):
    boss = adm(app, mk)
    for path in ["/admin", "/admin/users", "/admin/schools", "/admin/password-resets", "/admin/reports", "/admin/media", "/admin/payments",
                 "/admin/trivia", "/admin/flags", "/admin/audit"]:
        assert boss.get(path).status_code == 200, path


def test_role_cannot_be_self_assigned_at_registration(client, db, school):
    client.post("/register", data={"username": "sneaky", "display_name": "Sneaky", "email": "s@example.com", "school_id": school["id"], "password": "Sup3r-secret-pw", "password2": "Sup3r-secret-pw",
                                   "terms": "on", "age_ok": "on", "role": "admin", "account_status": "approved"})
    p = db.data["profiles"][0]
    assert p["role"] == "student" and p["account_status"] == "pending"


def test_user_moderation_actions_and_audit(app, as_user, db, mk):
    boss = adm(app, mk); c, p = as_user("alice")
    boss.post(f"/admin/users/{p['id']}/suspend", data={"note": "bullying"})
    assert c.get("/feed").status_code == 302                              # session revoked
    assert db.data["audit_logs"][-1]["action"] == "user_suspend"
    boss.post(f"/admin/users/{p['id']}/unsuspend")
    assert login(app.test_client(), "alice").status_code == 302
    me = db.data["profiles"][[x["username"] for x in db.data["profiles"]].index("boss")]
    boss.post(f"/admin/users/{me['id']}/suspend", data={"note": "x"})
    assert [x for x in db.data["profiles"] if x["username"] == "boss"][0]["account_status"] == "approved"   # can't suspend self


def test_report_review_removes_content_and_records_decision(app, as_user, db, mk):
    boss = adm(app, mk); a, _ = as_user("alice"); b, _ = as_user("bob")
    a.post("/posts", data={"caption": "bad post"}); pid = db.data["posts"][0]["id"]
    b.post("/report", data={"target_type": "post", "target_id": pid, "reason": "harassment"})
    rid = db.data["reports"][0]["id"]
    assert b"bad post" in boss.get("/admin/reports").data
    boss.post(f"/admin/reports/{rid}/remove", data={"note": "confirmed"})
    assert db.data["posts"][0]["moderation_status"] == "removed" and db.data["reports"][0]["status"] == "actioned"
    assert b"bad post" not in b.get("/feed").data and b.get(f"/post/{pid}").status_code == 404


def test_school_verification_flow_is_manual(app, as_user, db, mk, school):
    c, p = as_user("alice")
    c.post("/schools/kampala-intl/join")
    c.post("/schools/kampala-intl/verify-request", data={"evidence_note": "short"})
    assert not db.data.get("school_verification_requests")
    c.post("/schools/kampala-intl/verify-request", data={"evidence_note": "I am the deputy head, email deputy@kis.example"})
    assert db.data["schools"][0]["verified"] is False
    boss = adm(app, mk); rid = db.data["school_verification_requests"][0]["id"]
    boss.post(f"/admin/schools/requests/{rid}/approve", data={"note": "called the office"})
    assert db.data["schools"][0]["verified"] is True
    assert db.data["community_members"][0]["role"] == "moderator"


def test_announcements_only_by_moderators(app, as_user, db, mk, school):
    c, p = as_user("alice"); c.post("/schools/kampala-intl/join")
    c.post("/schools/kampala-intl/post", data={"caption": "fake announcement", "announcement": "on"})
    assert db.data["posts"][0]["is_announcement"] is False
    db.data["community_members"][0]["role"] = "moderator"
    c.post("/schools/kampala-intl/post", data={"caption": "real announcement", "announcement": "on"})
    assert db.data["posts"][1]["is_announcement"] is True
    outsider, _ = as_user("outsider")
    assert outsider.post("/schools/kampala-intl/post", data={"caption": "hi"}).status_code == 403


def test_error_pages(client):
    r = client.get("/definitely/not/here")
    assert r.status_code == 404 and b"Lost in orbit" in r.data
