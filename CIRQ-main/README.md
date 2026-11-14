# CIRQ — More than social. A place to stand out.

CIRQ is a social web app for students at international and secondary schools in East Africa: feed, reels, stories,
discover, school communities, LINKUP events, chats, the **Guess the Developer** card game (CIRQ FLIP), trivia,
app coins, boosts and CIRQ STATUS ranks, with an admin dashboard and safety tools.

> **Read this first — what has and hasn't been verified**
> * **209 automated tests pass** (`pytest`, run on 1 Nov 2026). They run against an in-memory stand-in for Supabase
>   (`tests/fakedb.py`) and a mocked Cloudinary upload call. Your real validation code, routes, templates and security rules
>   are exercised, but **no test has touched a live Supabase project, a live Cloudinary account, a real SMTP server or Render.**
>   Do the "first run checklist" in section 12 before inviting students.
> * The app was opened in a real headless browser (Chromium) against demo data to check the layout on phone and desktop.
> * **Real-money features are OFF by default** (`PAID_COMPETITIONS_ENABLED=false`) and enforced on the server. See section 15
>   before turning them on: they involve minors and need a legal review.

---

## 1. What's included

| Area | Status |
|---|---|
| Registration, username + password login (Supabase Auth), logout, remember-me, rate limiting, CSRF, secure cookies | Implemented, tested (mocked Supabase) |
| Admin approval of new accounts (pending / approved / rejected / suspended) | Implemented, tested |
| **Admin-approved password recovery** (request → admin review → single-use, expiring emailed link → reset → audit) + manual-review path | Implemented, tested |
| Feed: text/photo/video posts, edit/delete, likes, comments, follow, block, report, pagination | Implemented, tested |
| Reels (shared newest-first cursor archive, followed/private visibility, lazy playback, background feed refresh, in-page upload, likes/comments/saves), Stories (24 h, story tray, viewer, view tracking, cleanup job) | Implemented, tested (reels/stories UI only visually checked) |
| Profiles, settings, change password, **delete my account** | Implemented, tested |
| **Private accounts + follow requests** (Instagram-style: Follow → Requested → owner confirms/deletes; chat opens only after approval) | Implemented, tested (incl. headless-browser check) |
| **Story likes, liker list, and story replies that land in the owner's inbox** | Implemented, tested (incl. headless-browser check) |
| **Suggested for you** (learns from likes, comments, watch time, "Not interested"; explainable reasons; can be switched off/reset) | Implemented, tested |
| Discover (people / schools / popular / events, search, filters) | Implemented, tested |
| Schools, communities, announcements, community chat, school verification requests | Implemented, tested |
| Admin trivia authoring: CSV and text-based PDF import (numbered questions, wrapped/inline A-D options, trailing answer keys) | Implemented, tested; scanned PDFs require OCR before import |
| LINKUP events: create, RSVP, capacity, cancel/complete, event group chat, share link, report | Implemented, tested |
| Messaging: DMs + group chats, unread counts, search, attachments (private Cloudinary), polling | Implemented, tested |
| Notifications | Implemented, tested |
| **CIRQ FLIP — Guess the Developer** (collectible student cards, 5 guesses, server-side answer, 500 coins) | Implemented, tested |
| **App coins** (50 welcome coins, 500 for finding the developer, atomic ledger) + **boosts** | Implemented, tested |
| **Trivia** authored by admins, accuracy + speed scoring on the server clock, leaderboard | Implemented, tested |
| **Manual mobile-money payments** (register number → admin verifies → admin records payment → credit) | Implemented, tested, **disabled by default** |
| CIRQ STATUS ranks (exact ranges from the brief) + achievements | Implemented, tested |
| Admin dashboard: users, schools/verification, password resets, reports, media moderation, payments, trivia, flags, audit log | Implemented, tested |
| Supabase `schema.sql` with RLS, triggers and the coin function | Written; **not yet run against a live project by me** |
| Render config, Gunicorn, `/health` | Written; gunicorn boot verified locally, Render deploy not tested |

Not built (by design): voice/video calls, in-app payment processor, dating features.

## 2. Tech stack

Python 3.12 · Flask 3 (blueprints) · Jinja2 + vanilla JS + CSS · Supabase PostgreSQL + Supabase Auth · Cloudinary ·
Gunicorn · Render · pytest. Fonts: Inter (Google Fonts).

## 3. Project structure

```
cirq/
├── wsgi.py                 # entry point: gunicorn wsgi:app   (named wsgi.py because app/ is a package)
├── config.py               # all settings from environment variables; fails fast in production
├── schema.sql              # run once in the Supabase SQL editor
├── seed_data.py            # optional: add your schools from a text file
├── requirements.txt / requirements-dev.txt
├── Procfile / render.yaml / gunicorn.conf.py / .env.example / .gitignore
├── app/
│   ├── __init__.py         # app factory, security headers, CLI (create-admin, cleanup-expired)
│   ├── extensions.py  decorators.py  utils.py
│   ├── services/           # supabase, auth, cloudinary, mail, moderation, notification, coin, audit, social helpers
│   ├── blueprints/         # auth feed profiles discover reels stories linkup messages notifications
│   │                       # games schools wallet trivia admin legal
│   ├── templates/          # Jinja templates (partials/macros.html = reusable components)
│   └── static/css/style.css  js/app.js  images/
└── tests/                  # pytest suite + fakedb.py
```

## 4. Local setup (beginner friendly)

1. Install **Python 3.12** and **Git**.
2. In a terminal:
   ```bash
   cd cirq
   python -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   pip install -r requirements-dev.txt
   cp .env.example .env               # Windows: copy .env.example .env
   ```
3. Fill in `.env` (sections 5–8). Generate `SECRET_KEY` with:
   `python -c "import secrets;print(secrets.token_urlsafe(48))"`.

## 5. Supabase

1. Create a project at supabase.com (note the **database password**; you won't need it in the app).
2. **SQL Editor → New query →** paste all of `schema.sql` → **Run**. It creates every table, index, trigger, the
   `cirq_apply_coins` function, seed rows and Row Level Security. Re-running is safe. Order inside the file is
   extensions → tables → indexes/triggers → seed data → RLS.
   **Already have a project from an older `schema.sql`?** Don't re-create it: run
   `migrations/2026_10_31_story_likes_follow_requests_suggestions.sql` once instead (same SQL Editor, safe to re-run). It adds follow
   requests, story likes, the story-reply columns on `messages`, and the suggestion tables.
   **Then run `migrations/001_logos_music_story_comments.sql`** (also safe to re-run). It adds school logos, music on posts/stories,
   story comments, comment likes and the `story_comment` report type. A brand-new project already gets all of this from `schema.sql`.
   **Then run `migrations/2026_11_01_reel_archive.sql`** (safe to re-run) to add the index used by the unlimited reels archive.
   **Then run `migrations/2026_11_01_reel_saves.sql`** (safe to re-run) to add private reel bookmarks and the shared stream index.
   **Then run `migrations/2026_11_02_developer_card_settings.sql`** (safe to re-run) to add the independent editable CIRQ FLIP developer card.
   **Then run `migrations/2026_11_02_boost_ads.sql`** (safe to re-run) to allow boosted users to create sponsored ads for posts and reels feeds.
   **Then run `migrations/2026_11_03_school_submissions_and_approval.sql`** (safe to re-run) to add pending-school review, protected profile approval metadata, and the per-IP school-submission limiter.
   This migration's rollback notes are at its top. Do not remove its rate-limit function/table while the registration route is deployed.
   Reels are retained unless their creator deletes them; the archive loads five at a time, newest first, and refreshes new posts without resetting the current scroll position.
3. **Project Settings → API**: copy the Project URL into `SUPABASE_URL`, the anon/publishable key into
   `SUPABASE_ANON_KEY`, and the service-role/secret key into `SUPABASE_SERVICE_ROLE_KEY`.
   The service-role key is **server only**: never put it in a template, JavaScript or a screenshot.
4. **Authentication → Providers → Email**: sign-ups are created by the server (admin API), so you can turn **off**
   "Allow new users to sign up" for extra safety. You do not need Supabase's own confirmation emails.
   CIRQ does not impose password strength or username-based rules; passwords must be non-empty and confirmed. Supabase Auth
   may still enforce the minimum length and password policy configured for your project.
5. **Username login:** students log in with a username. The server finds the profile by username, reads the account's email
   from Supabase Auth with the service role, and verifies the password through Supabase Auth. Emails never reach the browser.
6. Add your schools: copy `schools.example.txt` to `schools.txt`, edit it, then `python seed_data.py schools.txt`
   (or add them later in **Admin → Schools**). Students can't register without a school to pick.

### Database access policy (documented)
The Flask server uses the service-role key and **authorizes every route itself** (login, ownership, membership, role). RLS is
enabled on every table as a second layer: if the anon key is ever used directly, a student could only see what they could
legitimately see in the app, and **nothing** from `password_reset_requests`, `email_verifications`, `audit_logs`,
`game_rounds`, `game_guesses`, `payer_numbers`, `payments`, `trivia_entry_credits`, `trivia_questions`, `trivia_answers`.
A trigger stops signed-in users editing their own `role`, `account_status`, `verified`, `coin_balance`, `boosted_until`.
Counters (`like_count`, `comment_count`) are maintained by triggers; follower counts are computed from the `follows` table.

## 6. Cloudinary

1. Create a free account → **Dashboard** shows *Cloud name*, *API Key*, *API Secret* → put them in `.env`.
2. That's it: uploads are made **server-side** with the secret (it never reaches the browser). Files go under `cirq/…`
   folders. Public media (posts, reels, stories, avatars, event covers) uses normal delivery with auto format/quality;
   existing chat media remains on authenticated Cloudinary delivery.
3. **New chat attachments use a separate private Supabase Storage bucket** (`chat-attachments`). Run
    [`migrations/2026_11_02_chat_attachments.sql`](./migrations/2026_11_02_chat_attachments.sql) in the Supabase SQL Editor
    after the main schema. The migration creates/configures the bucket as private (50 MiB hard ceiling per object; the
    application enforces the per-file defaults) and adds message
    attachment metadata plus participant/block-aware RLS. Do not make the bucket public or add browser read policies:
    Flask uses the server-only service role, checks chat membership and blocks, then redirects to short-lived signed URLs.
4. Supabase stores references to Cloudinary media and the bytes/metadata for new chat files in this private bucket.
    Chat upload limits default to 50 MiB per video and 25 MiB per other file. A message can include up to 10 files and
    85 MiB total; `CHAT_ATTACHMENT_VIDEO_MAX_MB`, `CHAT_ATTACHMENT_OTHER_MAX_MB`,
    `CHAT_ATTACHMENT_MAX_FILES`, and `CHAT_ATTACHMENT_BATCH_MAX_MB` can adjust those limits within Render's request ceiling.
    If you raise the video limit above 50 MiB, also raise the bucket's `file_size_limit` in Supabase; the platform-level
    Storage limit must allow it too. Flask accepts up to 95 MiB per request, below Render's 100 MB request-body limit.
5. **Cleanup policy:** deleting a post/story/account destroys its Cloudinary files. Expired stories are removed by the
    cleanup job (section 9). Limits: images 8 MB, videos 60 MB (`MAX_IMAGE_MB`, `MAX_VIDEO_MB`). Content is verified by real
    file signature, not just extension.

## 7. Email (password recovery + email verification)

Set `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USE_TLS`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER` for any SMTP provider
(Brevo, Mailgun, Gmail app password, …). **Until email is configured, admins cannot approve password resets** (the approval
is refused rather than silently succeeding) and verification emails are not sent. The reset email is never logged.

## 8. Create the first administrator

```bash
flask --app wsgi create-admin
```
It prompts for username, email, display name and a password typed at the prompt (not stored in any file). Admin rights can't
be granted through any registration field.

The hidden developer card in *Guess the Developer* is a game-only card, not a user account. Admins can set its name and image
under **Admin → Developer card**; no special developer account is required.

## 9. Run locally, test, schedule background jobs

```bash
flask --app wsgi run          # http://localhost:5000
pytest                        # 213 tests
flask --app wsgi cleanup-expired
```
**Cleanup job** (expired stories + Cloudinary files, stale reset links): `render.yaml` defines an hourly Render cron service. To use a
free scheduler instead, call `POST {APP_BASE_URL}/admin/internal/cleanup` hourly with header `Authorization: Bearer <CRON_SECRET>`.
Stories are also hidden by the database query the moment they expire, never just by the browser.

**Pending account approval:** Render runs a separate cron job every minute. Accounts still pending after the configured
`AUTO_APPROVE_AFTER_MINUTES` delay are automatically approved only if their CIRQ email-verification link was confirmed
and an administrator has not placed an auto-approval hold. Admin approval remains available sooner. Approval source and
timestamp are recorded, administrators are notified, and system-approved accounts appear under **Auto-approved (needs review)**.
The waiting page polls the same conditional approval service, so an eligible account is not stranded by a Render restart.
**Unlisted schools:** registration accepts a listed school or a school suggestion. New suggestions are rate-limited per
IP/day and grouped by normalized name. Admins can approve a school, merge it into an existing school, or reject it; rejected
students can choose a listed school in Settings after account approval. The migration stores only a keyed hash for the daily
IP cap, not the raw address.
Set `AUTO_APPROVAL_BLOCKED_EMAIL_DOMAINS` to a comma-separated list of banned or disposable email domains and
`AUTO_APPROVAL_BLOCKED_IPS` to IPs/CIDR ranges when your deployment has such a list; accounts matching either setting are
held for manual review. Admins can also place a per-account hold from **Admin → Users**.
Configure the `APP_BASE_URL` and `CRON_SECRET` values on both cron services. For another scheduler,
call `POST {APP_BASE_URL}/admin/internal/auto-approve-pending` every minute with header
`Authorization: Bearer $CRON_SECRET`.

## 10. GitHub

```bash
git init && git add . && git commit -m "CIRQ"
git branch -M main
git remote add origin https://github.com/<you>/cirq.git     # create the empty repo on github.com first
git push -u origin main
```
`.gitignore` already excludes `.env`. Check `git status` shows no `.env` before pushing.

## 11. Deploy on Render

1. render.com → **New → Blueprint** → connect the GitHub repo (it reads `render.yaml`), or **New → Web Service**
   with build `pip install -r requirements.txt` and start `gunicorn wsgi:app -c gunicorn.conf.py`, health check `/health`.
2. Fill the environment variables Render asks for (see `.env.example`): `APP_BASE_URL` = your Render URL
   (`https://<name>.onrender.com`), the three Supabase values, three Cloudinary values, the `MAIL_*` values.
   `SECRET_KEY` and `CRON_SECRET` are generated for you. Leave `PAID_COMPETITIONS_ENABLED=false`.
3. Deploy, open `/health`, then register and approve yourself with `create-admin` (run it from the Render **Shell** tab, or locally
   against the same Supabase project).
4. Rate limiting uses memory storage by default. With more than one worker/instance set `LIMITER_STORAGE_URI` to a Redis URL, otherwise
   limits are per-process.

## 12. First-run checklist (do this before real students use it)

- [ ] `schema.sql` ran with no errors; tables show in Supabase.
- [ ] `create-admin` works; you can log in and see **Admin**.
- [ ] Register a test student → appears under **Admin → Users** → approve → they receive 50 coins.
- [ ] Upload a photo, a reel (MP4) and a story; confirm files appear in Cloudinary and posts show them.
- [ ] Send a chat attachment; confirm the URL contains a signature and doesn't open without it.
- [ ] Configure email, then run the full password recovery flow with the test student.
- [ ] With ≥ 5 other approved, public students, play Guess the Developer once.
- [ ] Look at it on a real phone.

## 13. How the main flows work

**Private accounts (Instagram flow).** *Settings → Account privacy → Private account.* While private: tapping **Follow** sends a request
and the button reads **Requested** (tap again to cancel); the owner gets a "requested to follow you" alert and a **Follow requests** row at the
top of Activity (`/follow-requests`) with **Confirm** / **Delete**. Confirming creates the follow and notifies the requester. Until then the
requester can't see posts, stories or reels, can't open the story viewer, and **can't start or continue a chat** with the owner (the Message button
is locked and the thread shows why). The owner can always message people they follow. Existing followers stay; public posts become followers-only;
switching back to public approves everyone who was waiting. Blocking someone clears any pending request. Private accounts are never recommended
to strangers.

**Story likes, activity and replies.** Viewers get a heart and a reply box under every story. A like notifies the owner once (unliking withdraws an
unread alert). The owner (only) sees a view and like count on the story and can open **Story activity** with **Viewers** / **Likes** tabs listing
exactly who. A reply is delivered as a normal direct message flagged "Replied to your story" (with a thumbnail while the story is live), so it shows up
in the owner's **Messages** inbox with the unread badge. The story pauses while the viewer types. Likes/replies follow the same rules as viewing:
expired, removed, blocked or private-and-unapproved stories return 404.

**Password recovery.** Student gives username, school and the registered email (or "I can't use that email" + their name + a note) →
a `pending` request is stored (no password or token) and the screen *always* says the same thing, so usernames can't be probed →
admin reviews under **Admin → Password resets**, approves or rejects with a note → on approval the server emails a single-use link
(30 min, only its SHA-256 hash is stored) to the verified email on file → student sets a new password → token burned, request
`completed`, all sessions revoked, notification email sent, audit entry written. Admins never see or set passwords. **Manual
review** (lost email, or email never verified): the admin must record how they verified the student (e.g. phoned the school
coordinator) and enter a replacement email; the link goes there and the account email is updated on completion.

**Guess the Developer.** On *Deal the cards* the server builds a deck of 6–12 face-down cards: real approved, **public**, non-blocked
students plus the developer, shuffles it with `secrets`, and stores the deck and the developer's position **only in the database**.
The browser gets just the number of cards. Flipping a card (5 guesses, no repeats, validated server-side) reveals that student's
card (name, school, real follower count and CIRQ STATUS tier set the colour). Finding the developer offers a **500 coin** claim
button; coins are added when claimed, once per round (ledger-protected), limited to `DEV_CARD_REWARDS_PER_DAY` (default **1**)
so coins can't be farmed. Unclaimed wins survive a refresh and must be claimed before starting another round. If fewer than 5
other students exist the game says so. It never invents users.
Admins can update the developer card's name and image under **Admin → Developer card**; these are stored independently from user profiles.

**Coins & boosts.** Every coin change goes through the database function `cirq_apply_coins` (atomic, idempotent, never negative).
50 welcome coins on approval. 1000 coins buy a 3-day boost (Boosted badge + listed first in Discover). While boosted, users can create
a custom sponsored ad with an image, headline, and text; it rotates through other students' posts and reels feeds and stops when the
boost expires. Boosts never affect CIRQ STATUS.

**Trivia.** Admin builds a set (**Admin → Trivia**), publishes it. Per question: 100 points if right + up to 100 speed bonus; time is
measured from the moment the server served the question, late answers score 0, answers are shown only after the set is finished. Coins =
points ÷ 20 (max 100), credited the moment the set is finished, **free or paid**. The ledger reference is the attempt id and there is one
attempt per student per set, so a refresh or double-submit can never pay twice. (Total coins are bounded by how many sets the admin publishes.)

**School logos.** **Admin → Schools**: add a logo when creating a school, or use *Upload / Replace / Remove logo* on any school row. The logo replaces the
cap icon on the school community page, the Schools list and Discover → Schools. Schools without a logo keep the cap icon. Logos are fitted (never cropped)
and the old file is deleted from Cloudinary when replaced. The Cloudinary API key must be allowed to upload (see section 6).

**Music on posts and stories.** The post composer and the story sheet have an *Add music* picker (MP3, M4A, AAC, WAV or OGG; `MAX_AUDIO_MB`, default 10;
`MAX_AUDIO_SECONDS`, default 90) and an optional song title. Files are checked by real content signature, uploaded to Cloudinary (audio is stored as
resource type `video`) and always delivered as MP3. Posts show a play button (one clip plays at a time); story music starts when the story opens, with a chip
to pause it. Deleting the post/story or the account removes the clip. **Copyright:** users upload their own files; CIRQ has no licensed music library, so keep
the report button and moderation in place, and consider adding a rule to the Guidelines page.

**Live camera.** Every photo/video picker has a camera button (post composer, story sheet, new reel). It opens a full-screen camera (`static/js/camera.js`)
with photo and video modes, front/back switch, a recording timer with a limit (30 s story, 60 s post, 90 s reel) and a retake/use screen. The result goes into the
normal upload field, so server validation is unchanged. Needs HTTPS (Render provides it) and the browser's camera permission; the app now sends
`Permissions-Policy: camera=(self), microphone=(self)`. Safari records MP4, Chrome/Firefox WebM; both are accepted. Browsers with no camera API fall back to
the phone's own camera app.

**Story comments and comment likes.** Stories have a comment button next to the heart; comments are visible to everyone who may see the story (blocks, private
accounts and expiry are enforced exactly like story likes), the story owner can delete any comment, and comments can be reported. Comments on posts and on
stories can be liked; the comment's author gets one "liked your comment" notification.

### Account recovery code

Every new student gets a simple personal code (like `K7M2-9QXD-4TFH`: 12 letters and numbers, no look-alike characters) straight after signing up, and must tick "I've saved my recovery code". Students who joined before this feature see a reminder banner until they create theirs.

- **Using it:** *Forgot password → Use code* (or `/recover`). Username (or email) + code + new password. No admin needed. Any case, with or without dashes, is accepted.
- **Finding it later:** *Settings → Account recovery* (or the *Recovery code* button on your own profile). The code stays hidden until the student re-enters their password, so a borrowed unlocked phone can't reveal it. They can also make a new code there (the old one stops working).
- **After it is used** the password changes, every device is signed out, the student is emailed and notified, and a **new code replaces the used one** (each code works once).
- **Safety:** a code alone is useless: recovery needs the username/email too. Five wrong tries in a row lock recovery for 30 minutes (`RECOVERY_MAX_ATTEMPTS`, `RECOVERY_LOCK_MINUTES`). The plaintext is never stored: the table keeps an HMAC (to check codes) and an encrypted copy (so the student can view their own), in a table the public database key cannot read. The existing admin-reviewed reset still works for students who lost their code.

**Set it up:** run `migrations/2026_11_11_account_recovery_code.sql`, install the new dependency (`pip install -r requirements.txt`, adds `cryptography`), and set **`RECOVERY_CODE_KEY`** to a long random string (e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`). **Never change it afterwards**: it protects every stored code, and changing it makes existing codes unusable (students would need to make new ones). If you don't set it, a key is derived from `SECRET_KEY`, so rotating `SECRET_KEY` would have the same effect.

### Coin gifts and loans between friends

Students who follow each other can **gift** coins (instant, final) or **lend** them with a signed agreement. Open a friend's profile and tap the gift icon, or go to *App coins → Gifts & loans*.

**How a loan works**

1. The lender picks the amount, a due date and a late fee (0–20%). The borrower sees the agreement in plain words and taps **I agree**. The agreement text and its SHA-256 fingerprint are stored permanently with both people and the timestamps. Coins move only on agreement; an unanswered offer lapses after 48 hours; either side can withdraw/decline first.
2. Reminders go to the borrower 3 days before, 1 day before and on the due day.
3. At the end of the due day (Kampala time) the app takes what the borrower has, up to what is owed, and pays the lender. Both are notified.
4. If the borrower is short, a **one-time late fee** (the lender's % of the *unpaid part only*, never compounded) is added and goes to the lender. From then on **50% of every coin the borrower earns** (trivia, FLIP, achievements…) goes to the lender until it is cleared. Gifts from friends and admin corrections are never taken (coins can't be bought with money in CIRQ, only earned).
5. Borrowers can pay back early; lenders can forgive at any time; admins can freeze a loan (*Admin → Loans*).

**Set it up**

- Run `migrations/2026_11_10_coin_gifts_and_loans.sql` in the Supabase SQL editor (adds three tables and the atomic `cirq_transfer_coins` function).
- Collection is done by the **`cirq-lending`** cron in `render.yaml` (every 5 minutes). Without a Render cron, call this from any scheduler: `curl -X POST -H "Authorization: Bearer $CRON_SECRET" $APP_BASE_URL/admin/internal/lending-run`. As a safety net, a student's own loans are also settled whenever they open their wallet.
- Limits and switches are environment variables: `LENDING_ENABLED`, `GIFT_MAX_COINS`, `GIFT_DAILY_COINS`, `GIFT_DAILY_COUNT`, `LOAN_MIN_COINS`, `LOAN_MAX_COINS`, `LOAN_MIN_DAYS`, `LOAN_MAX_DAYS`, `LOAN_MAX_FEE_PCT`, `LOAN_DEFAULT_FEE_PCT`, `LOAN_OFFER_HOURS`, `LOAN_GARNISH_PCT`, `LOAN_OFFERS_PER_DAY`.

**Why it can't double-charge:** every coin movement is one atomic database call keyed by a unique reference, and every loan event is keyed by `(loan_id, ref)`. A repeated cron run, a double click or a crash half-way is repaired, not repeated (see `tests/test_lending.py`).

> Coins can't be bought directly, but they can be spent on account boosts (which also have a UGX price), so they carry some indirect value, and many of your students are minors. Charging a late fee on loans between students may fall under lending rules in your country, so please take legal advice before enabling loans publicly. Set `LOAN_MAX_FEE_PCT=0` for fee-free lending, or `LENDING_ENABLED=false` to turn the feature off.

## 14. Manual mobile-money payments (disabled by default)

No in-app payment processor is used. Flow: student registers the number and the name that shows when they send money (+ confirms
they're 18+ or have a guardian's permission) → admin verifies it → student sends money to the admin's number → admin sees the
money arrive and records it with the mobile-money transaction ID (each ID can only ever be credited once) → the system adds
a trivia entry or starts a 3-/5-day boost. Prices: trivia UGX 1,000; boost 3 days UGX 1,000; boost 5 days UGX 7,000
(edit with `BOOST_3D_PRICE_UGX`, `BOOST_5D_PRICE_UGX`, `TRIVIA_ENTRY_FEE_UGX`). Phone numbers are visible only to admins.

## 15. Before you switch real money on — please read

`PAID_COMPETITIONS_ENABLED` stays **false** until you've done these, because CIRQ's users are minors:
* **Legal review in Uganda.** Paid trivia entry where points convert to coins that can buy a boost of equal value can look like a
  paid contest with prizes. Check the gaming/lotteries rules and consumer rules that apply, and your data-protection obligations
  for storing phone numbers.
* **Child safety:** the guardian/age checkbox is self-attested. Consider real parental consent for under-18s.
* **Reconcile payments yourself** against your mobile-money statement; the app trusts what an admin records.
* Re-check pricing: "UGX 7,000 for 5 days" is configured exactly as specified but is much more per day than the 3-day plan.
  Change it if that wasn't intended.

## 16. Environment variables

See `.env.example` for the full list with comments. Required in production: `SECRET_KEY` (≥ 32 chars), `APP_BASE_URL`,
`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `CLOUDINARY_*`. The app refuses to start in production if any is missing.

### CIRQ LIVE (streaming, gifts, PK battles)
Live streaming is documented in **[LIVE_STREAMING.md](LIVE_STREAMING.md)**: run `migrations/2026_11_14_live_streaming.sql`, set `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, and run `scripts/vendor_livekit.sh`. Until the provider is configured the Live pages load but nobody can go live.

## 17. Troubleshooting

| Symptom | Fix |
|---|---|
| `CIRQ cannot start. Missing environment variables…` | Set the listed variables (`.env` locally, Render dashboard in production). |
| Can't pick a school when registering | Add schools (seed script or Admin → Schools). |
| "Media uploads aren't configured" | Cloudinary variables missing/wrong. |
| Approving a password reset says email could not be sent | Fix `MAIL_*`; check provider logs and the sender address. |
| Logged in but bounced to "pending" | An admin can approve the account sooner; otherwise it is approved automatically after five minutes. |
| "CIRQ FLIP needs a few more students" | Needs ≥ 5 other approved **public** students. |
| Login always fails | Check `SUPABASE_ANON_KEY`; the user must exist in Supabase Auth **and** `profiles`. |
| `permission denied` / empty results from Supabase | You used the anon key where the service-role key belongs. |
| Rate-limit errors behind several workers | Set `LIMITER_STORAGE_URI` (Redis). |

## 18. Known limitations

* Live integrations (Supabase, Cloudinary, SMTP, Render) are **untested by me**; the tests use fakes. The fake mirrors the schema's
  defaults, unique rules, counter triggers and coin function, but a real database can still differ, so run the checklist in section 12.
* Private accounts hide posts, reels, stories and chat until approved, but (like Instagram) the name, photo, bio, school and follower counts stay visible so people can find the account to request it. Stories shown in the tray come only from people you follow.
* Story comments are public to the story's viewers (Instagram has none; if you prefer owner-only, change `comments()` in `stories/routes.py`).
  Music is user-uploaded, not licensed: there is no built-in song library. Reels don't have a music picker.
* Story likes are one-tap hearts (no reaction types) and story replies are text only (no attachments). Story views/likes disappear with the story after 24 hours.
* "Suggested for you" learns only from on-platform behaviour and needs a little activity before it's accurate; it is rule-based (explainable), not a trained model.
* Reel view counts aren't tracked (no consistent way to count them yet). One media file per post.
* Chat uses 4-second polling, not Supabase Realtime. Some pages make several small queries (fine for thousands of students, not millions).
* Email isn't verified at sign-up unless SMTP is configured; unverified accounts use the manual-review recovery path.
* The basic text filter (a few severe terms + phone numbers) is not a substitute for human moderation. Media is not scanned automatically.
* Hero and login artwork are cropped from your promo posters (`static/images/hero-students.jpg`, `auth-*.jpg`): make sure you hold the rights, or replace them.
* Fonts load from Google Fonts; images/videos from Cloudinary.
* Not a legal document: the Guidelines and Privacy pages are plain-language drafts for you to review.
