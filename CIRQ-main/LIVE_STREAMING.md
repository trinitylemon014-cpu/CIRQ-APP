# CIRQ LIVE: setup, operation and limits

Live video is carried by **LiveKit (WebRTC)**. Flask never touches video: it signs short-lived room tokens, enforces rules, and runs the coin ledger.

## 1. What you must do before anyone can go live
1. **Run the migration** `migrations/2026_11_14_live_streaming.sql` in the Supabase SQL editor. It only adds tables, functions and columns (`create ... if not exists`, `add column if not exists`); it deletes or resets nothing. It is also appended to `schema.sql` for fresh installs.
2. **Create a LiveKit project** (LiveKit Cloud or self-hosted) and copy its WebSocket URL, API key and API secret.
3. **Set env vars on Render** (Environment tab): `LIVEKIT_URL` (`wss://...`), `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`. The secret is server-only and is never put in a page.
4. **Vendor the browser SDK** (the CSP allows only `script-src 'self'`, so no CDN): run `scripts/vendor_livekit.sh`, commit `app/static/vendor/livekit-client.umd.min.js`, redeploy. Without it the room shows "Video player isn't installed".
5. **Choose who may host** with `LIVE_ELIGIBILITY`: `verified` (default: verified-badge users, or admin-granted in Admin -> Live) or `approved` (any approved, email-verified student; good for testing).
6. Redeploy. Admin -> Live shows whether the provider is configured.

## 2. Environment variables
| Var | Default | Meaning |
|---|---|---|
| `LIVE_ENABLED` | true | Master switch (pages and API return 404 when off) |
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | empty | Provider credentials. Empty = nobody can go live, with a clear message |
| `LIVE_ELIGIBILITY` | verified | `verified` or `approved` |
| `LIVE_MAX_GUESTS` | 4 | Capped at 4 |
| `LIVE_STARTS_PER_HOUR` | 6 | Per-host start limit |
| `LIVE_POLL_SECONDS` | 3 | Chat/event poll interval |
| `LIVE_CHAT_MAX_CHARS` / `LIVE_CHAT_PER_MINUTE` / `LIVE_REACTIONS_PER_MINUTE` | 200 / 20 / 30 | Chat limits |
| `LIVE_GIFT_DAILY_COINS` | 20000 | Max coins one student can spend on live gifts per 24h (0 = no cap) |
| `LIVE_HOST_TIMEOUT_SECONDS` / `LIVE_VIEWER_TIMEOUT_SECONDS` | 90 / 45 | Dead-host reaping and presence |
| `LIVE_BATTLE_COUNTDOWN_SECONDS` / `LIVE_BATTLE_INVITE_SECONDS` | 5 / 60 | PK timings |
| `LIVE_NOTIFY_MAX` | 500 | Followers notified per live |

## 3. How it works
- **Video/audio**: browser <-> LiveKit directly. Hosts get publish rights; accepted guests get only what the host allows (revocable live via LiveKit's API); everyone else is watch-only. Identities are opaque HMACs, never account ids.
- **Realtime chat/events**: an append-only `live_events` table polled every few seconds (cursor-based). This keeps the existing gunicorn + Supabase architecture (no websocket server). Practical ceiling: roughly **100 viewers per room** at a 3s poll; beyond that, move events to Supabase Realtime or LiveKit data channels.
- **Gifts/coins**: one Postgres function (`cirq_live_send_gift`) does everything atomically: locks wallets in a fixed order, checks the daily cap, debits the sender, credits the host exactly 50%, records the platform's 50%, writes ledger rows, updates fan club, supporter totals and (during a PK) battle score. Idempotent per `(sender, request_id)`, so a double tap or network retry never charges twice. The browser never decides amounts.
- **Gift table**: Red Rose 10/5, Love Heart 50/25, Mystery Box 100/50, Golden Crown 500/250, Treasure Chest 1000/500, Sports Car 2000/1000, Diamond Ring 5000/2500, Private Jet 10000/5000 (price/host earns).
- **Treasure Chest diamonds**: 10 diamonds at 100 each are an animation/display record only (`live_diamond_records`, constrained so they can never count toward earnings). The host is credited exactly 500 coins.
- **PK battles**: host-to-host invite, consent, countdown, timed round; only gifts sent while the battle is `active` (server clock) score. Viewers see both streams via a second watch-only room connection.
- **Safety/moderation**: mute, timeout, remove, block, moderators, delete and pin messages, slow mode, chat on/off, reports (admin queue), all persisted (`live_moderation_actions`, `live_reports`) and admin actions audited. Guests must consent before they can publish.
- **Money rules**: coins are in-app only. There is **no cash-out** of any kind, so nothing is implemented for minors or adults.

## 4. Two-account test guide (no real money)
1. Admin: set the three LiveKit vars, vendor the SDK, deploy. Admin -> Live should say "connected". Grant account A hosting access (or set `LIVE_ELIGIBILITY=approved`).
2. Browser 1 (account A, host): Live -> Go live -> Check camera & microphone -> Start. Allow camera/mic.
3. Browser 2 (account B, viewer, give B coins via admin tools): Live -> open A's live. You should see/hear A; chat both ways.
4. B -> gift icon -> Red Rose: balance drops 10; A's wallet +5; animation shows on both; A's Creator earnings shows gross 10 / yours 5 / CIRQ 5.
5. Mystery Box: B pays 100 once, A gets 50, opening animation, no double charge if you double-click.
6. Treasure Chest: B pays 1000, A gets exactly 500; "10 diamonds x 100 (display only)" is shown but not added.
7. B -> Ask to join; A accepts in Host controls; B is asked to confirm, then publishes. A can mute/remove B at any time.
8. A -> Host controls -> invite another live host to PK; accept on their side; send gifts from viewers and watch the score bar.
9. As a moderator/host: tap a chat name -> mute / timeout / remove / report. Check Admin -> Live for reports.
10. A ends the live; viewers see "This live has ended".

## 5. Test status (be honest about it)
- Automated: SQL money/concurrency tests against real Postgres (`tests/test_live_sql.py`, needs `CIRQ_TEST_PG`), plus route/service/page tests (`tests/test_live.py`, `tests/test_live_pages.py`) against the in-memory test DB.
- **Not tested here**: a real LiveKit server, real cameras/microphones, the real browser SDK, real Supabase PostgREST. Video/guest/PK media paths are written against LiveKit's documented API and must be verified on your deployment with the guide above before you tell users it works.

## 6. Known limitations
- Polling realtime (about 100 viewers per room).
- No replay/recording (not implemented; would need LiveKit Egress and storage).
- Weekly "competitions" are rankings only (no prizes).
- A creator's wallet shows coins earned; there is no payout system.
- The LiveKit client script must be vendored by you (step 4).
