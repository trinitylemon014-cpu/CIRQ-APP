-- =====================================================================================
-- CIRQ database schema for Supabase PostgreSQL
-- Run the whole file once in: Supabase Dashboard -> SQL Editor -> New query -> Run.
-- It is idempotent for tables/indexes (IF NOT EXISTS) so it is safe to re-run.
-- Order: extensions -> tables -> indexes -> triggers -> seed data -> RLS policies.
--
-- ACCESS MODEL
--   * The Flask server uses the SERVICE ROLE key (bypasses RLS) and performs its own authorization
--     on every route. The browser never talks to the database directly.
--   * RLS is still enabled on every table. If the anon/publishable key is ever used directly, the
--     policies below expose only what a signed-in student could legitimately see, and nothing at all
--     for sensitive tables (password resets, audit logs, game answers, reports).
-- =====================================================================================

create extension if not exists "pgcrypto";

-- ---------- Schools & communities ----------
create table if not exists schools (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  slug text not null unique,
  country text not null default 'Uganda',
  city text,
  about text,
  verified boolean not null default false,       -- set only by administrators after review
  created_at timestamptz not null default now()
);

create table if not exists communities (
  id uuid primary key default gen_random_uuid(),
  school_id uuid unique references schools(id) on delete cascade,
  name text not null,
  description text,
  created_at timestamptz not null default now()
);

-- ---------- Profiles (1:1 with auth.users) ----------
create table if not exists profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  username text not null unique check (username = lower(username) and username ~ '^[a-z0-9][a-z0-9_.]{1,22}[a-z0-9]$'),
  display_name text not null,
  school_id uuid references schools(id) on delete set null,
  bio text check (char_length(bio) <= 200),
  avatar_url text,
  avatar_public_id text,
  role text not null default 'student' check (role in ('student','moderator','admin')),
  account_status text not null default 'pending' check (account_status in ('pending','approved','rejected','suspended')),
  verified boolean not null default false,       -- identity/school verification; never popularity
  is_private boolean not null default false,
  show_active_status boolean not null default true,
  email_verified_at timestamptz,
  session_epoch integer not null default 0,      -- bump to sign out every existing session
  coin_balance integer not null default 0 check (coin_balance >= 0),   -- cached from coin_ledger; server only
  boosted_until timestamptz,                     -- paid/coin boost; never affects follower tiers
  terms_accepted_at timestamptz not null default now(),
  status_note text,                              -- admin-visible reason for rejection/suspension
  last_seen_at timestamptz,
  created_at timestamptz not null default now()
);
alter table profiles add column if not exists last_seen_at timestamptz;
alter table profiles add column if not exists show_active_status boolean not null default true;
create index if not exists profiles_school_idx on profiles(school_id);
create index if not exists profiles_status_idx on profiles(account_status);
create unique index if not exists profiles_username_lower_uidx on profiles(lower(username));
create index if not exists profiles_active_status_idx on profiles(last_seen_at desc) where show_active_status;

-- Supabase owns auth.users and its indexes; only query it through the service-side function below.
create or replace function public.cirq_profile_id_for_email(p_email text)
returns table(user_id uuid)
language sql
stable
security definer
set search_path = ''
as $$
  select u.id
  from auth.users u
  join public.profiles p on p.id = u.id
  where lower(u.email) = lower(trim(p_email))
  limit 1
$$;
revoke all on function public.cirq_profile_id_for_email(text) from public, anon, authenticated;
grant execute on function public.cirq_profile_id_for_email(text) to service_role;

create or replace function public.cirq_ping_presence(p_user_id uuid)
returns boolean
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  changed integer;
begin
  update public.profiles
  set last_seen_at = now()
  where id = p_user_id
    and show_active_status
    and (last_seen_at is null or last_seen_at <= now() - interval '30 seconds');
  get diagnostics changed = row_count;
  return changed = 1;
end
$$;
revoke all on function public.cirq_ping_presence(uuid) from public, anon, authenticated;
grant execute on function public.cirq_ping_presence(uuid) to service_role;

create table if not exists email_verifications (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id) on delete cascade,
  token_hash text not null unique,
  expires_at timestamptz not null,
  used_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists school_verification_requests (
  id uuid primary key default gen_random_uuid(),
  school_id uuid not null references schools(id) on delete cascade,
  requested_by uuid not null references profiles(id) on delete cascade,
  evidence_note text not null,                   -- e.g. official school email domain, staff contact
  status text not null default 'pending' check (status in ('pending','approved','rejected')),
  reviewed_by uuid references profiles(id),
  reviewed_at timestamptz,
  decision_note text,
  created_at timestamptz not null default now()
);

create table if not exists community_members (
  community_id uuid not null references communities(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  role text not null default 'member' check (role in ('member','moderator')),
  created_at timestamptz not null default now(),
  primary key (community_id, user_id)
);

-- ---------- Social graph ----------
create table if not exists follows (
  follower_id uuid not null references profiles(id) on delete cascade,
  followee_id uuid not null references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (follower_id, followee_id),
  check (follower_id <> followee_id)
);
create index if not exists follows_followee_idx on follows(followee_id);

-- A follow request is pending until the owner of a PRIVATE account approves it (then a row is written to
-- follows and the request is deleted) or declines it (request just deleted). Until approval the requester
-- sees nothing private and cannot start a chat with the account.
create table if not exists follow_requests (
  requester_id uuid not null references profiles(id) on delete cascade,
  target_id uuid not null references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (requester_id, target_id),
  check (requester_id <> target_id)
);
create index if not exists follow_requests_target_idx on follow_requests(target_id, created_at desc);

create table if not exists blocks (
  blocker_id uuid not null references profiles(id) on delete cascade,
  blocked_id uuid not null references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (blocker_id, blocked_id),
  check (blocker_id <> blocked_id)
);

-- ---------- Posts & reels share one table; files live in Cloudinary ----------
create table if not exists posts (
  id uuid primary key default gen_random_uuid(),
  author_id uuid not null references profiles(id) on delete cascade,
  kind text not null default 'post' check (kind in ('post','reel')),
  caption text check (char_length(caption) <= 2000),
  visibility text not null default 'public' check (visibility in ('public','followers')),
  community_id uuid references communities(id) on delete cascade,
  is_announcement boolean not null default false,
  moderation_status text not null default 'ok' check (moderation_status in ('ok','flagged','removed')),
  like_count integer not null default 0,         -- maintained by triggers below
  comment_count integer not null default 0,
  created_at timestamptz not null default now(),
  edited_at timestamptz
);
create index if not exists posts_feed_idx on posts(kind, moderation_status, created_at desc);
create index if not exists posts_author_idx on posts(author_id, created_at desc);
create index if not exists posts_community_idx on posts(community_id, created_at desc);
create index if not exists posts_public_reels_library_idx on posts(created_at desc, id desc)
  where kind = 'reel' and visibility = 'public' and moderation_status = 'ok' and community_id is null;
create index if not exists posts_reel_stream_idx on posts(created_at desc, id desc)
  where kind = 'reel' and moderation_status = 'ok' and community_id is null;

create table if not exists post_media (
  id uuid primary key default gen_random_uuid(),
  post_id uuid not null references posts(id) on delete cascade,
  owner_id uuid not null references profiles(id) on delete cascade,
  media_type text not null check (media_type in ('image','video')),
  url text not null,
  public_id text not null,                       -- Cloudinary public id, used for deletion/transformations
  delivery_type text not null default 'upload',
  bytes bigint, width integer, height integer, duration numeric,
  created_at timestamptz not null default now()
);
create index if not exists post_media_post_idx on post_media(post_id);

create table if not exists likes (
  post_id uuid not null references posts(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (post_id, user_id)
);

create table if not exists saved_posts (
  post_id uuid not null references posts(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (post_id, user_id)
);
create index if not exists saved_posts_user_idx on saved_posts(user_id, created_at desc);

create table if not exists comments (
  id uuid primary key default gen_random_uuid(),
  post_id uuid not null references posts(id) on delete cascade,
  author_id uuid not null references profiles(id) on delete cascade,
  body text not null check (char_length(body) between 1 and 500),
  moderation_status text not null default 'ok' check (moderation_status in ('ok','flagged','removed')),
  created_at timestamptz not null default now()
);
create index if not exists comments_post_idx on comments(post_id, created_at);


-- ---------- Stories (expire after 24h; also removed from Cloudinary by the cleanup job) ----------
create table if not exists stories (
  id uuid primary key default gen_random_uuid(),
  author_id uuid not null references profiles(id) on delete cascade,
  media_type text not null check (media_type in ('image','video')),
  url text not null,
  public_id text not null,
  delivery_type text not null default 'upload',
  caption text check (char_length(caption) <= 150),
  moderation_status text not null default 'ok' check (moderation_status in ('ok','flagged','removed')),
  expires_at timestamptz not null,
  created_at timestamptz not null default now()
);
create index if not exists stories_active_idx on stories(expires_at);
create index if not exists stories_author_idx on stories(author_id, created_at desc);

create table if not exists story_views (
  story_id uuid not null references stories(id) on delete cascade,
  viewer_id uuid not null references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (story_id, viewer_id)
);

-- Story likes. Only the story's owner ever sees who liked (and how many); rows vanish with the story.
create table if not exists story_likes (
  story_id uuid not null references stories(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (story_id, user_id)
);
create index if not exists story_likes_story_idx on story_likes(story_id, created_at desc);

-- ---------- LINKUP events ----------
create table if not exists events (
  id uuid primary key default gen_random_uuid(),
  host_id uuid not null references profiles(id) on delete cascade,
  school_id uuid references schools(id) on delete set null,
  title text not null check (char_length(title) between 3 and 120),
  description text check (char_length(description) <= 2000),
  venue text not null check (char_length(venue) <= 160),   -- a named venue only; no home addresses
  starts_at timestamptz not null,
  ends_at timestamptz,
  capacity integer check (capacity is null or capacity > 0),
  visibility text not null default 'public' check (visibility in ('public','school')),
  status text not null default 'scheduled' check (status in ('scheduled','cancelled','completed')),
  moderation_status text not null default 'ok' check (moderation_status in ('ok','flagged','removed')),
  cover_url text, cover_public_id text,
  conversation_id uuid,
  created_at timestamptz not null default now()
);
create index if not exists events_start_idx on events(starts_at);

create table if not exists event_rsvps (
  event_id uuid not null references events(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  status text not null check (status in ('going','interested')),
  created_at timestamptz not null default now(),
  primary key (event_id, user_id)
);

-- ---------- Messaging ----------
create table if not exists conversations (
  id uuid primary key default gen_random_uuid(),
  kind text not null check (kind in ('dm','group')),
  title text,
  dm_key text unique,                            -- "<smaller-uuid>:<larger-uuid>" prevents duplicate DMs
  event_id uuid references events(id) on delete cascade,
  community_id uuid references communities(id) on delete cascade,
  created_by uuid references profiles(id) on delete set null,
  created_at timestamptz not null default now()
);

create table if not exists conversation_members (
  conversation_id uuid not null references conversations(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  last_read_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  primary key (conversation_id, user_id)
);
create index if not exists conv_members_user_idx on conversation_members(user_id);

create table if not exists messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references conversations(id) on delete cascade,
  sender_id uuid not null references profiles(id) on delete cascade,
  body text check (char_length(body) <= 2000),
  media_type text check (media_type in ('image','video')),
  media_url text, media_public_id text,
  moderation_status text not null default 'ok' check (moderation_status in ('ok','flagged','removed')),
  created_at timestamptz not null default now(),
  check (body is not null or media_public_id is not null)
);
create index if not exists messages_conv_idx on messages(conversation_id, created_at desc);

-- Delivery ticks and per-user disappearing messages (see migrations/2026_11_14_chat_ticks_disappearing.sql)
alter table messages add column if not exists delivered_at timestamptz;
alter table conversation_members add column if not exists disappear_after integer check (disappear_after is null or disappear_after > 0);
create table if not exists message_hides (
  id uuid primary key default gen_random_uuid(),
  message_id uuid not null references messages(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  conversation_id uuid not null references conversations(id) on delete cascade,
  hide_at timestamptz not null,
  unique (message_id, user_id)
);
create index if not exists message_hides_user_idx on message_hides(user_id, hide_at);
create index if not exists message_hides_conv_idx on message_hides(conversation_id, hide_at);
alter table message_hides enable row level security;

-- A reply to someone's story arrives as a normal direct message flagged with the story it answers.
-- story_id becomes NULL once the story expires/deletes; story_reply stays true so the chat can still say
-- "Replied to your story".
alter table messages add column if not exists story_id uuid references stories(id) on delete set null;
alter table messages add column if not exists story_reply boolean not null default false;

alter table events drop constraint if exists events_conversation_fk;
alter table events add constraint events_conversation_fk foreign key (conversation_id) references conversations(id) on delete set null;

-- ---------- Notifications, reports, audit ----------
create table if not exists notifications (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id) on delete cascade,
  actor_id uuid references profiles(id) on delete set null,
  kind text not null,
  ref_type text, ref_id text,
  body text,
  read_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists notifications_user_idx on notifications(user_id, created_at desc);

create table if not exists reports (
  id uuid primary key default gen_random_uuid(),
  reporter_id uuid not null references profiles(id) on delete cascade,
  target_type text not null check (target_type in ('post','comment','profile','message','event','story','reel')),
  target_id text not null,
  reason text not null check (reason in ('harassment','spam','inappropriate','impersonation','unsafe','other')),
  details text check (char_length(details) <= 500),
  status text not null default 'open' check (status in ('open','actioned','dismissed')),
  resolved_by uuid references profiles(id),
  resolution_note text,
  resolved_at timestamptz,
  created_at timestamptz not null default now(),
  unique (reporter_id, target_type, target_id)
);
create index if not exists reports_status_idx on reports(status, created_at desc);

create table if not exists password_reset_requests (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id) on delete cascade,
  status text not null default 'pending' check (status in ('pending','approved','rejected','completed','expired')),
  manual_review boolean not null default false,  -- true when the student can't use their verified email
  submitted_details jsonb not null default '{}'::jsonb,   -- what the student typed; no passwords
  request_ip_hash text,
  reviewed_by uuid references profiles(id),
  reviewed_at timestamptz,
  decision_note text,                            -- internal; never shown to the student
  delivery_email_hash text,
  token_hash text unique,                        -- SHA-256 of the one-time token; plaintext is never stored
  token_expires_at timestamptz,
  token_used_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists pw_reset_status_idx on password_reset_requests(status, created_at desc);

-- ---------- ACCOUNT RECOVERY CODE ----------
-- Every student gets one simple code (e.g. K7M2-9QXD-4TFH) at sign-up. If they forget their password they can reset it with
-- their username/email + this code. The server keeps it two ways: an HMAC (to check a submitted code) and an AES-encrypted copy
-- (so the student can look their own code up again after re-entering their password). The plaintext is never stored.
-- This table has RLS on and NO policies: nobody but the server's service role can read or write it.
alter table profiles add column if not exists recovery_saved_at timestamptz;     -- when the student confirmed they saved it (drives the reminder banner)

create table if not exists recovery_codes (
  user_id uuid primary key references profiles(id) on delete cascade,
  code_hash text not null unique,        -- HMAC-SHA256 of the normalised code
  code_enc text not null,                -- Fernet (AES-128-CBC + HMAC) ciphertext of the code
  created_at timestamptz not null default now(),
  last_used_at timestamptz,
  failed_attempts integer not null default 0,
  locked_until timestamptz               -- too many wrong tries: recovery is paused until then
);
alter table recovery_codes enable row level security;
revoke all on recovery_codes from anon, authenticated;

create table if not exists audit_logs (
  id uuid primary key default gen_random_uuid(),
  actor_id uuid references profiles(id) on delete set null,
  action text not null,
  target_type text, target_id text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists audit_created_idx on audit_logs(created_at desc);

create table if not exists feature_flags (
  key text primary key,
  enabled boolean not null default false,
  description text,
  updated_by uuid references profiles(id),
  updated_at timestamptz not null default now()
);

create table if not exists boost_ads (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique references profiles(id) on delete cascade,
  headline text not null check (char_length(headline) between 2 and 80),
  body text not null check (char_length(body) between 5 and 300),
  image_url text not null,
  image_public_id text not null,
  updated_at timestamptz not null default now()
);
create index if not exists boost_ads_updated_idx on boost_ads(updated_at desc);

-- ---------- CIRQ FLIP & achievements ----------
create table if not exists developer_card_settings (
  id text primary key default 'developer' check (id = 'developer'),
  display_name text,
  image_url text,
  image_public_id text,
  updated_by uuid references profiles(id) on delete set null,
  updated_at timestamptz not null default now()
);
insert into developer_card_settings(id) values ('developer') on conflict (id) do nothing;

create table if not exists game_rounds (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id) on delete cascade,
  answer_index integer not null check (answer_index between 0 and 11),   -- SERVER ONLY; never sent to clients
  deck jsonb not null default '[]'::jsonb,       -- SERVER ONLY: which real student sits behind each face-down card
  status text not null default 'active' check (status in ('active','won','lost')),
  guesses_used integer not null default 0 check (guesses_used between 0 and 5),
  created_at timestamptz not null default now(),
  ended_at timestamptz
);
create unique index if not exists one_active_round_per_user on game_rounds(user_id) where status = 'active';

create table if not exists game_guesses (
  id uuid primary key default gen_random_uuid(),
  round_id uuid not null references game_rounds(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  card_index integer not null check (card_index between 0 and 11),
  correct boolean not null,
  created_at timestamptz not null default now(),
  unique (round_id, card_index)                  -- no repeated guesses within a round
);

create table if not exists achievements (
  code text primary key,
  name text not null,
  description text not null,
  icon text not null default 'star'
);

create table if not exists user_achievements (
  user_id uuid not null references profiles(id) on delete cascade,
  code text not null references achievements(code) on delete cascade,
  awarded_at timestamptz not null default now(),
  primary key (user_id, code)
);


-- ---------- APP COINS (append-only ledger; balance is a cache written only by cirq_apply_coins) ----------
create table if not exists coin_ledger (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id) on delete cascade,
  amount integer not null check (amount <> 0),
  reason text not null,          -- welcome | dev_card | trivia | achievement | boost_spend | admin_adjust | gift_out | gift_in | loan_out | loan_in | loan_repay_out | loan_repay_in
  ref text not null default '',  -- idempotency key (e.g. game round id)
  created_at timestamptz not null default now(),
  unique (user_id, reason, ref)  -- the same reward can never be granted twice
);
create index if not exists coin_ledger_user_idx on coin_ledger(user_id, created_at desc);

-- Atomic grant/spend. Returns the new balance, or -1 if it was a duplicate, or -2 if funds are insufficient.
create or replace function cirq_apply_coins(p_user uuid, p_amount integer, p_reason text, p_ref text)
returns integer language plpgsql security definer as $$
declare new_balance integer; inserted integer;
begin
  perform 1 from profiles where id = p_user for update;
  if p_amount < 0 and (select coin_balance from profiles where id = p_user) + p_amount < 0 then return -2; end if;
  insert into coin_ledger(user_id, amount, reason, ref) values (p_user, p_amount, p_reason, coalesce(p_ref, ''))
    on conflict (user_id, reason, ref) do nothing;
  get diagnostics inserted = row_count;
  if inserted = 0 then return -1; end if;
  update profiles set coin_balance = coin_balance + p_amount where id = p_user returning coin_balance into new_balance;
  return new_balance;
end $$;
revoke all on function cirq_apply_coins(uuid, integer, text, text) from public, anon, authenticated;

-- ---------- COIN GIFTS & LOANS ----------
-- Gifts are instant and final. Loans are a two-party agreement: the lender offers, the borrower accepts ("I agree"),
-- coins move only on acceptance, and on the due date the app collects automatically (see app/services/lending.py).
-- Every movement of coins goes through cirq_transfer_coins (atomic, idempotent on ref); the loan's own history is
-- the append-only coin_loan_events table, whose (loan_id, ref) key makes a retried collection impossible to double-count.

-- Atomic two-sided transfer. Returns the sender's new balance, -1 if this ref was already processed, -2 if the sender
-- has too few coins, -3 for an invalid request. Both rows are locked in a fixed order so two transfers can't deadlock.
create or replace function cirq_transfer_coins(p_from uuid, p_to uuid, p_amount integer, p_reason_out text, p_reason_in text, p_ref text)
returns integer language plpgsql security definer as $$
declare inserted integer; sender_balance integer;
begin
  if p_amount is null or p_amount <= 0 or p_from = p_to then return -3; end if;
  if p_from < p_to then
    perform 1 from profiles where id = p_from for update; perform 1 from profiles where id = p_to for update;
  else
    perform 1 from profiles where id = p_to for update; perform 1 from profiles where id = p_from for update;
  end if;
  if (select coin_balance from profiles where id = p_from) < p_amount then return -2; end if;
  insert into coin_ledger(user_id, amount, reason, ref) values (p_from, -p_amount, p_reason_out, coalesce(p_ref, ''))
    on conflict (user_id, reason, ref) do nothing;
  get diagnostics inserted = row_count;
  if inserted = 0 then return -1; end if;
  insert into coin_ledger(user_id, amount, reason, ref) values (p_to, p_amount, p_reason_in, coalesce(p_ref, ''))
    on conflict (user_id, reason, ref) do nothing;
  update profiles set coin_balance = coin_balance - p_amount where id = p_from returning coin_balance into sender_balance;
  update profiles set coin_balance = coin_balance + p_amount where id = p_to;
  return sender_balance;
end $$;
revoke all on function cirq_transfer_coins(uuid, uuid, integer, text, text, text) from public, anon, authenticated;

create table if not exists coin_gifts (
  id uuid primary key default gen_random_uuid(),
  sender_id uuid not null references profiles(id) on delete cascade,
  recipient_id uuid not null references profiles(id) on delete cascade,
  amount integer not null check (amount between 1 and 100000),
  message text check (char_length(message) <= 200),
  created_at timestamptz not null default now(),
  check (sender_id <> recipient_id)
);
create index if not exists coin_gifts_sender_idx on coin_gifts(sender_id, created_at desc);
create index if not exists coin_gifts_recipient_idx on coin_gifts(recipient_id, created_at desc);

create table if not exists coin_loans (
  id uuid primary key default gen_random_uuid(),
  lender_id uuid not null references profiles(id) on delete cascade,
  borrower_id uuid not null references profiles(id) on delete cascade,
  amount integer not null check (amount between 1 and 100000),
  fee_pct integer not null default 20 check (fee_pct between 0 and 20),   -- late fee, charged once on the unpaid part at the due date
  due_at timestamptz not null,                                            -- end of the chosen day, Kampala time
  message text check (char_length(message) <= 200),
  status text not null default 'offered' check (status in ('offered','active','overdue','repaid','declined','cancelled','expired','forgiven')),
  expires_at timestamptz not null,                                        -- an unanswered offer lapses after 48 hours
  accepted_at timestamptz,
  fee_amount integer not null default 0 check (fee_amount >= 0),          -- fixed once, at the due date
  repaid_total integer not null default 0 check (repaid_total >= 0),      -- principal first, then the fee
  forgiven_amount integer not null default 0 check (forgiven_amount >= 0),
  overdue_at timestamptz,
  closed_at timestamptz,
  frozen boolean not null default false,                                  -- admin safety switch: no collection while true
  terms jsonb not null,                                                   -- the agreement exactly as both people saw it
  terms_hash text not null,                                               -- sha256 of that agreement
  created_at timestamptz not null default now(),
  check (lender_id <> borrower_id)
);
create index if not exists coin_loans_lender_idx on coin_loans(lender_id, status);
create index if not exists coin_loans_borrower_idx on coin_loans(borrower_id, status);
create index if not exists coin_loans_status_due_idx on coin_loans(status, due_at);

create table if not exists coin_loan_events (
  id uuid primary key default gen_random_uuid(),
  loan_id uuid not null references coin_loans(id) on delete cascade,
  kind text not null,            -- offered | accepted | declined | cancelled | expired | reminder_3d | reminder_1d | reminder_due | payment | overdue | fee_applied | repaid | forgiven | frozen | unfrozen
  amount integer not null default 0,
  actor_id uuid references profiles(id) on delete set null,
  note text,
  ref text not null,             -- idempotency key
  created_at timestamptz not null default now(),
  unique (loan_id, ref)
);
create index if not exists coin_loan_events_loan_idx on coin_loan_events(loan_id, created_at);

alter table coin_gifts enable row level security;
alter table coin_loans enable row level security;
alter table coin_loan_events enable row level security;
drop policy if exists gifts_own on coin_gifts; create policy gifts_own on coin_gifts for select to authenticated using (sender_id = auth.uid() or recipient_id = auth.uid());
drop policy if exists loans_parties on coin_loans; create policy loans_parties on coin_loans for select to authenticated using (lender_id = auth.uid() or borrower_id = auth.uid());
drop policy if exists loan_events_parties on coin_loan_events; create policy loan_events_parties on coin_loan_events for select to authenticated
  using (exists (select 1 from coin_loans l where l.id = loan_id and (l.lender_id = auth.uid() or l.borrower_id = auth.uid())));

-- ---------- MANUAL MOBILE-MONEY PAYMENTS (no in-app payment processor) ----------
-- 1) A student registers the number + the name that appears when they send money.
-- 2) An admin verifies it. 3) Each time money arrives from that number the admin records the payment,
--    which grants the matching entitlement (trivia entry credit or boost). Students never self-credit.
create table if not exists payer_numbers (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id) on delete cascade,
  phone text not null check (phone ~ '^\+[0-9]{9,15}$'),
  account_name text not null check (char_length(account_name) between 2 and 80),
  network text not null default 'mobile_money' check (network in ('mtn','airtel','mobile_money')),
  guardian_confirmed boolean not null default false,   -- student is 18+ or has a parent/guardian's permission
  status text not null default 'pending' check (status in ('pending','approved','rejected','revoked')),
  reviewed_by uuid references profiles(id),
  reviewed_at timestamptz,
  note text,
  created_at timestamptz not null default now()
);
create unique index if not exists payer_phone_active on payer_numbers(phone) where status in ('pending','approved');
create index if not exists payer_user_idx on payer_numbers(user_id);

create table if not exists payments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id) on delete cascade,
  payer_number_id uuid not null references payer_numbers(id),
  amount_ugx integer not null check (amount_ugx > 0),
  purpose text not null check (purpose in ('trivia_entry','boost_3d','boost_5d')),
  provider_reference text not null unique,       -- mobile-money transaction ID: blocks double-crediting
  recorded_by uuid not null references profiles(id),
  note text,
  created_at timestamptz not null default now()
);

create table if not exists trivia_entry_credits (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id) on delete cascade,
  payment_id uuid references payments(id) on delete set null,
  used_set_id uuid,
  used_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists entry_credits_user_idx on trivia_entry_credits(user_id, used_at);

create table if not exists boosts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id) on delete cascade,
  source text not null check (source in ('coins','payment')),
  payment_id uuid references payments(id) on delete set null,
  starts_at timestamptz not null,
  ends_at timestamptz not null,
  created_at timestamptz not null default now()
);

-- ---------- TRIVIA (questions are authored by admins; correct answers never leave the server) ----------
create table if not exists trivia_sets (
  id uuid primary key default gen_random_uuid(),
  title text not null check (char_length(title) between 3 and 100),
  description text check (char_length(description) <= 500),
  status text not null default 'draft' check (status in ('draft','published','closed')),
  created_by uuid references profiles(id) on delete set null,
  created_at timestamptz not null default now(),
  published_at timestamptz
);

create table if not exists trivia_questions (
  id uuid primary key default gen_random_uuid(),
  set_id uuid not null references trivia_sets(id) on delete cascade,
  position integer not null,
  prompt text not null check (char_length(prompt) between 3 and 300),
  options jsonb not null check (jsonb_typeof(options) = 'array' and jsonb_array_length(options) = 4),
  correct_index integer not null check (correct_index between 0 and 3),   -- SERVER ONLY
  time_limit_seconds integer not null default 20 check (time_limit_seconds between 5 and 120),
  unique (set_id, position)
);

create table if not exists trivia_attempts (
  id uuid primary key default gen_random_uuid(),
  set_id uuid not null references trivia_sets(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  status text not null default 'in_progress' check (status in ('in_progress','completed')),
  paid boolean not null default false,
  current_position integer not null default 1,
  question_served_at timestamptz,                -- server clock; speed is measured here, not in the browser
  correct_count integer not null default 0,
  total_questions integer not null default 0,
  accuracy numeric not null default 0,
  points integer not null default 0,
  coins_awarded integer not null default 0,
  started_at timestamptz not null default now(),
  completed_at timestamptz,
  unique (set_id, user_id)                       -- one attempt per student per set
);
create index if not exists trivia_attempts_rank_idx on trivia_attempts(set_id, points desc);

create table if not exists trivia_answers (
  id uuid primary key default gen_random_uuid(),
  attempt_id uuid not null references trivia_attempts(id) on delete cascade,
  question_id uuid not null references trivia_questions(id) on delete cascade,
  choice integer,                                -- null = timed out
  correct boolean not null,
  ms_taken integer not null,
  points integer not null default 0,
  created_at timestamptz not null default now(),
  unique (attempt_id, question_id)
);

-- ---------- Triggers: derived counters (never user-editable) ----------
create or replace function cirq_bump_post_counters() returns trigger language plpgsql as $$
begin
  if tg_table_name = 'likes' then
    if tg_op = 'INSERT' then update posts set like_count = like_count + 1 where id = new.post_id;
    else update posts set like_count = greatest(like_count - 1, 0) where id = old.post_id; end if;
  elsif tg_table_name = 'comments' then
    if tg_op = 'INSERT' then update posts set comment_count = comment_count + 1 where id = new.post_id;
    else update posts set comment_count = greatest(comment_count - 1, 0) where id = old.post_id; end if;
  end if;
  return null;
end $$;

drop trigger if exists likes_counter on likes;
create trigger likes_counter after insert or delete on likes for each row execute function cirq_bump_post_counters();
drop trigger if exists comments_counter on comments;
create trigger comments_counter after insert or delete on comments for each row execute function cirq_bump_post_counters();

-- Users may never change their own role, status, verification, or counters.
create or replace function cirq_protect_profile_columns() returns trigger language plpgsql as $$
begin
  if auth.role() <> 'service_role' then
    new.role := old.role; new.account_status := old.account_status; new.verified := old.verified;
    new.session_epoch := old.session_epoch; new.email_verified_at := old.email_verified_at;
    new.status_note := old.status_note; new.id := old.id;
    new.coin_balance := old.coin_balance; new.boosted_until := old.boosted_until;
  end if;
  return new;
end $$;
drop trigger if exists profiles_protect on profiles;
create trigger profiles_protect before update on profiles for each row execute function cirq_protect_profile_columns();

-- ---------- Seed data ----------
insert into feature_flags(key, enabled, description) values
  ('registrations_open', true,  'Allow new students to register'),
  ('reels_enabled',      true,  'Show CIRQ Reels'),
  ('stories_enabled',    true,  'Show CIRQ Stories'),
  ('linkup_enabled',     true,  'Show CIRQ LINKUP events'),
  ('games_enabled',      true,  'Show CIRQ FLIP')
on conflict (key) do nothing;
-- NOTE: paid competitions are NOT a database flag. They are controlled only by the server environment
-- variable PAID_COMPETITIONS_ENABLED (default false) so they cannot be switched on from the dashboard.

insert into achievements(code, name, description, icon) values
  ('first_post',   'First Spark',     'Share your first post',                   'edit'),
  ('first_follow', 'Connected',       'Follow your first student',               'users'),
  ('first_follower','Noticed',        'Get your first follower',                 'heart'),
  ('storyteller',  'Storyteller',     'Share your first story',                  'circle'),
  ('reel_maker',   'Reel Maker',      'Upload your first reel',                  'film'),
  ('event_host',   'Host',            'Create a LINKUP event',                   'calendar'),
  ('joiner',       'Joiner',          'RSVP to a LINKUP event',                  'check'),
  ('flip_first_win','Sharp Eye',      'Win your first CIRQ FLIP round',          'zap'),
  ('flip_five',    'Regular',         'Play five CIRQ FLIP rounds',              'layers'),
  ('flip_streak',  'On a Roll',       'Win three CIRQ FLIP rounds',              'award'),
  ('community',    'Community Member','Join your school community',              'home'),
  ('dev_finder',   'Found the Dev',   'Find the developer card in CIRQ FLIP',    'code'),
  ('trivia_first', 'Quiz Starter',    'Finish your first trivia set',            'help'),
  ('trivia_ace',   'Perfect Score',   'Answer every question in a set correctly','target'),
  ('booster',      'In the Spotlight','Boost your account',                      'rocket')
on conflict (code) do nothing;

-- =====================================================================================
-- ROW LEVEL SECURITY
-- =====================================================================================
do $$
declare t text;
begin
  for t in select unnest(array[
    'schools','communities','profiles','email_verifications','school_verification_requests','community_members',
    'follows','follow_requests','blocks','posts','post_media','likes','comments','stories',
    'story_views','story_likes','events','event_rsvps',
    'conversations','conversation_members','messages','notifications','reports','password_reset_requests',
    'audit_logs','feature_flags','developer_card_settings','boost_ads','game_rounds','game_guesses','achievements','user_achievements',
    'coin_ledger','payer_numbers','payments','trivia_entry_credits','boosts','trivia_sets','trivia_questions',
    'trivia_attempts','trivia_answers'])
  loop execute format('alter table %I enable row level security', t); end loop;
end $$;

-- Helper used by messaging policies
create or replace function cirq_is_member(conv uuid) returns boolean language sql security definer stable as $$
  select exists (select 1 from conversation_members where conversation_id = conv and user_id = auth.uid());
$$;

-- Public reference data
drop policy if exists schools_read on schools;       create policy schools_read on schools for select using (true);
drop policy if exists achievements_read on achievements; create policy achievements_read on achievements for select using (true);
drop policy if exists flags_read on feature_flags;   create policy flags_read on feature_flags for select using (true);
drop policy if exists communities_read on communities; create policy communities_read on communities for select to authenticated using (true);

-- Profiles: a student can read approved profiles and edit only their own row (sensitive columns are
-- locked by the profiles_protect trigger). Email lives only in auth.users and is never exposed here.
drop policy if exists profiles_read on profiles;
create policy profiles_read on profiles for select to authenticated using (account_status = 'approved' or id = auth.uid());
drop policy if exists profiles_update_own on profiles;
create policy profiles_update_own on profiles for update to authenticated using (id = auth.uid()) with check (id = auth.uid());

-- Posts & comments
drop policy if exists posts_read on posts;
create policy posts_read on posts for select to authenticated using (
  moderation_status = 'ok' and (visibility = 'public' or author_id = auth.uid()
    or exists (select 1 from follows f where f.follower_id = auth.uid() and f.followee_id = posts.author_id)));
drop policy if exists posts_write on posts;
create policy posts_write on posts for insert to authenticated with check (author_id = auth.uid());
drop policy if exists posts_update_own on posts;
create policy posts_update_own on posts for update to authenticated using (author_id = auth.uid()) with check (author_id = auth.uid());
drop policy if exists posts_delete_own on posts;
create policy posts_delete_own on posts for delete to authenticated using (author_id = auth.uid());

drop policy if exists post_media_read on post_media;
create policy post_media_read on post_media for select to authenticated using (exists (select 1 from posts p where p.id = post_id and p.moderation_status = 'ok'));
drop policy if exists likes_read on likes;      create policy likes_read on likes for select to authenticated using (true);
drop policy if exists likes_own on likes;       create policy likes_own on likes for all to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());
drop policy if exists comments_read on comments; create policy comments_read on comments for select to authenticated using (moderation_status = 'ok');
drop policy if exists comments_write on comments; create policy comments_write on comments for insert to authenticated with check (author_id = auth.uid());
drop policy if exists comments_delete_own on comments; create policy comments_delete_own on comments for delete to authenticated using (author_id = auth.uid());

-- Follows / blocks
drop policy if exists follows_read on follows;  create policy follows_read on follows for select to authenticated using (true);
drop policy if exists follows_own on follows;   create policy follows_own on follows for all to authenticated using (follower_id = auth.uid()) with check (follower_id = auth.uid());
drop policy if exists blocks_own on blocks;     create policy blocks_own on blocks for all to authenticated using (blocker_id = auth.uid()) with check (blocker_id = auth.uid());
-- Follow requests: only the two people involved can see one; the requester files and may withdraw it,
-- the target may decline it. Approval (writing the follows row) is done by the server only.
drop policy if exists freq_read on follow_requests;
create policy freq_read on follow_requests for select to authenticated using (requester_id = auth.uid() or target_id = auth.uid());
drop policy if exists freq_insert on follow_requests;
create policy freq_insert on follow_requests for insert to authenticated with check (requester_id = auth.uid());
drop policy if exists freq_delete on follow_requests;
create policy freq_delete on follow_requests for delete to authenticated using (requester_id = auth.uid() or target_id = auth.uid());

-- Stories: only unexpired, approved stories
drop policy if exists stories_read on stories;
create policy stories_read on stories for select to authenticated using (expires_at > now() and moderation_status = 'ok');
drop policy if exists stories_own on stories;   create policy stories_own on stories for all to authenticated using (author_id = auth.uid()) with check (author_id = auth.uid());
drop policy if exists story_views_own on story_views;
create policy story_views_own on story_views for all to authenticated using (viewer_id = auth.uid()) with check (viewer_id = auth.uid());
-- Story likes: a person manages their own like; a story's owner may read the likes on their stories.
drop policy if exists story_likes_own on story_likes;
create policy story_likes_own on story_likes for all to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());
drop policy if exists story_likes_owner_read on story_likes;
create policy story_likes_owner_read on story_likes for select to authenticated
  using (exists (select 1 from stories s where s.id = story_id and s.author_id = auth.uid()));

-- Events
drop policy if exists events_read on events;
create policy events_read on events for select to authenticated using (moderation_status = 'ok');
drop policy if exists events_write on events;   create policy events_write on events for insert to authenticated with check (host_id = auth.uid());
drop policy if exists events_update_own on events; create policy events_update_own on events for update to authenticated using (host_id = auth.uid()) with check (host_id = auth.uid());
drop policy if exists rsvps_read on event_rsvps; create policy rsvps_read on event_rsvps for select to authenticated using (true);
drop policy if exists rsvps_own on event_rsvps;  create policy rsvps_own on event_rsvps for all to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());

-- Communities
drop policy if exists cm_read on community_members; create policy cm_read on community_members for select to authenticated using (true);
drop policy if exists cm_own on community_members;  create policy cm_own on community_members for all to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid() and role = 'member');
drop policy if exists svr_own on school_verification_requests;
create policy svr_own on school_verification_requests for select to authenticated using (requested_by = auth.uid());

-- Messaging: members only
drop policy if exists conv_read on conversations;  create policy conv_read on conversations for select to authenticated using (cirq_is_member(id));
drop policy if exists cmem_read on conversation_members; create policy cmem_read on conversation_members for select to authenticated using (cirq_is_member(conversation_id));
drop policy if exists msg_read on messages;  create policy msg_read on messages for select to authenticated using (cirq_is_member(conversation_id) and moderation_status = 'ok');
drop policy if exists msg_write on messages; create policy msg_write on messages for insert to authenticated with check (sender_id = auth.uid() and cirq_is_member(conversation_id));

-- Notifications & achievements: own rows
drop policy if exists notif_own on notifications; create policy notif_own on notifications for select to authenticated using (user_id = auth.uid());
drop policy if exists ua_read on user_achievements; create policy ua_read on user_achievements for select to authenticated using (true);

-- Reports: reporters may file, nobody can read them back through the API
drop policy if exists reports_insert on reports; create policy reports_insert on reports for insert to authenticated with check (reporter_id = auth.uid());

drop policy if exists coins_own on coin_ledger; create policy coins_own on coin_ledger for select to authenticated using (user_id = auth.uid());
drop policy if exists trivia_sets_read on trivia_sets; create policy trivia_sets_read on trivia_sets for select to authenticated using (status <> 'draft');
drop policy if exists attempts_own on trivia_attempts; create policy attempts_own on trivia_attempts for select to authenticated using (user_id = auth.uid());
drop policy if exists boosts_own on boosts; create policy boosts_own on boosts for select to authenticated using (user_id = auth.uid());

-- No policies (deny all for anon/authenticated; only the server's service role may access):
--   password_reset_requests, recovery_codes, email_verifications, audit_logs, game_rounds, game_guesses,
--   payer_numbers (private phone numbers), payments, trivia_entry_credits, trivia_questions (hold the answers),
--   trivia_answers. Reads of these always go through the Flask server.


-- ---------- Personalised suggestions (what a student likes -> similar posts) ----------
-- Only on-platform behaviour is used. Students can switch it off or reset it in Settings.
alter table profiles add column if not exists personalized_recs boolean not null default true;

create table if not exists post_signals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id) on delete cascade,
  post_id uuid not null references posts(id) on delete cascade,
  author_id uuid not null references profiles(id) on delete cascade,
  event text not null check (event in ('impression','view','watch','like','comment','share','hide')),
  value numeric not null default 0,                -- view: dwell ms, watch: completion ratio (1 = watched fully)
  created_at timestamptz not null default now()
);
create index if not exists post_signals_user_idx on post_signals(user_id, created_at desc);
create index if not exists post_signals_user_event_idx on post_signals(user_id, event, created_at desc);

create table if not exists user_interests (
  user_id uuid not null references profiles(id) on delete cascade,
  dim text not null check (dim in ('author','topic','school','kind')),
  key text not null,
  weight double precision not null default 0,      -- decays with a 14-day half-life when read
  updated_at timestamptz not null default now(),
  primary key (user_id, dim, key)
);
create index if not exists user_interests_user_idx on user_interests(user_id, updated_at desc);

alter table post_signals enable row level security;      -- no policies: only the server (service role) can touch these
alter table user_interests enable row level security;


-- =====================================================================================
-- v2 ADDITIONS (same content as migrations/001_logos_music_story_comments.sql).
-- Fresh project: this is already included, nothing else to run.
-- =====================================================================================
-- ---------- 1. School logos (shown on the school community page instead of the cap icon) ----------
alter table schools add column if not exists logo_url text;
alter table schools add column if not exists logo_public_id text;

-- ---------- 2. Music on posts and stories ----------
-- One short audio clip per post / story. The file lives in Cloudinary; these columns are the reference.
alter table posts   add column if not exists audio_url text;
alter table posts   add column if not exists audio_public_id text;
alter table posts   add column if not exists audio_title text check (char_length(audio_title) <= 80);
alter table posts   add column if not exists audio_duration numeric;

alter table stories add column if not exists audio_url text;
alter table stories add column if not exists audio_public_id text;
alter table stories add column if not exists audio_title text check (char_length(audio_title) <= 80);
alter table stories add column if not exists audio_duration numeric;

-- ---------- 3. Story comments ----------
-- Visible to everyone who is allowed to see the story (the server enforces blocks and private accounts).
-- Rows vanish with the story (24 h), so nothing lingers.
create table if not exists story_comments (
  id uuid primary key default gen_random_uuid(),
  story_id uuid not null references stories(id) on delete cascade,
  author_id uuid not null references profiles(id) on delete cascade,
  body text not null check (char_length(body) between 1 and 500),
  moderation_status text not null default 'ok' check (moderation_status in ('ok','flagged','removed')),
  created_at timestamptz not null default now()
);
create index if not exists story_comments_story_idx on story_comments(story_id, created_at);

-- ---------- 4. Likes on comments (post comments and story comments) ----------
create table if not exists comment_likes (
  comment_id uuid not null references comments(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (comment_id, user_id)
);
create index if not exists comment_likes_comment_idx on comment_likes(comment_id);

create table if not exists story_comment_likes (
  comment_id uuid not null references story_comments(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (comment_id, user_id)
);
create index if not exists story_comment_likes_comment_idx on story_comment_likes(comment_id);

-- ---------- 5. Reports can now point at a story comment ----------
alter table reports drop constraint if exists reports_target_type_check;
alter table reports add constraint reports_target_type_check
  check (target_type in ('post','comment','profile','message','event','story','reel','story_comment'));

-- ---------- 6. Row Level Security (second layer; the Flask server authorizes every request itself) ----------
alter table story_comments enable row level security;
alter table comment_likes enable row level security;
alter table story_comment_likes enable row level security;
alter table saved_posts enable row level security;

-- Story comments: a person reads their own comments and the comments on their own stories.
drop policy if exists story_comments_read on story_comments;
create policy story_comments_read on story_comments for select to authenticated
  using (author_id = auth.uid()
         or exists (select 1 from stories s where s.id = story_id and s.author_id = auth.uid()));
drop policy if exists story_comments_write on story_comments;
create policy story_comments_write on story_comments for insert to authenticated with check (author_id = auth.uid());
drop policy if exists story_comments_delete on story_comments;
create policy story_comments_delete on story_comments for delete to authenticated using (author_id = auth.uid());

-- Comment likes: like counts are not sensitive (same as post likes); a person manages only their own like.
drop policy if exists comment_likes_read on comment_likes;
create policy comment_likes_read on comment_likes for select to authenticated using (true);
drop policy if exists comment_likes_own on comment_likes;
create policy comment_likes_own on comment_likes for all to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());

drop policy if exists story_comment_likes_own on story_comment_likes;
create policy story_comment_likes_own on story_comment_likes for all to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());

drop policy if exists saved_posts_own on saved_posts;
create policy saved_posts_own on saved_posts for all to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());

-- ---------- 7. Text stories ----------
-- A story can now be written text on a colour background instead of a photo or video. A text story has no file, so
-- url / public_id may be empty for it; the content check below guarantees every story has either a file or text.
alter table stories add column if not exists text_body text;
alter table stories add column if not exists bg_style text;
alter table stories alter column url drop not null;
alter table stories alter column public_id drop not null;
alter table stories drop constraint if exists stories_media_type_check;
alter table stories add constraint stories_media_type_check check (media_type in ('image','video','text'));
alter table stories drop constraint if exists stories_text_body_len;
alter table stories add constraint stories_text_body_len check (text_body is null or char_length(text_body) between 1 and 280);
alter table stories drop constraint if exists stories_bg_style_check;
alter table stories add constraint stories_bg_style_check check (bg_style is null or bg_style in ('violet','ocean','sunset','forest','rose','midnight','gold','mono'));
alter table stories drop constraint if exists stories_content_check;
alter table stories add constraint stories_content_check check (
  (media_type = 'text' and text_body is not null) or (media_type in ('image','video') and url is not null and public_id is not null));

-- Make PostgREST pick up the new tables and columns right away.
notify pgrst, 'reload schema';
