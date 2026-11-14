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

-- ---------- CIRQ LIVE (same as migrations/2026_11_14_live_streaming.sql) ----------
-- CIRQ LIVE: live streaming, chat, guests, gifting, PK battles, creator earnings.  Safe to run more than once.
-- Run in the Supabase SQL editor (or psql) BEFORE deploying the matching code.
--
-- What this does NOT do: it never touches existing balances, ledger rows or user data. New coin movements use the existing
-- coin_ledger + profiles.coin_balance (reasons 'live_gift_out' / 'live_gift_in'), written by cirq_live_send_gift in the
-- same transaction as the gift record, so there is no second wallet.
--
-- Rollback (only if no live data matters):  drop table live_* cascade;  drop function cirq_live_*;
--   alter table profiles drop column live_host_granted, drop column live_notify_enabled, drop column live_show_in_viewer_list;

-- ---------- profile columns ----------
alter table profiles add column if not exists live_host_granted boolean not null default false;        -- admin grants hosting rights
alter table profiles add column if not exists live_notify_enabled boolean not null default true;       -- tell me when creators I follow go live
alter table profiles add column if not exists live_show_in_viewer_list boolean not null default true;  -- appear in a room's viewer list

-- Signed-in users may not grant themselves hosting rights (the server uses the service role, which is allowed).
create or replace function cirq_protect_live_columns() returns trigger language plpgsql as $$
begin
  if auth.role() <> 'service_role' then new.live_host_granted := old.live_host_granted; end if;
  return new;
end $$;
drop trigger if exists profiles_protect_live on profiles;
create trigger profiles_protect_live before update on profiles for each row execute function cirq_protect_live_columns();

insert into feature_flags(key, enabled, description) values ('live_enabled', true, 'Show CIRQ LIVE')
  on conflict (key) do nothing;

-- ---------- gift catalogue (prices live in the database; the browser never sends a price) ----------
create table if not exists live_gifts (
  code text primary key check (code ~ '^[a-z_]{2,30}$'),
  name text not null,
  price integer not null check (price > 0),
  host_share_pct integer not null default 50 check (host_share_pct between 1 and 100),
  tier text not null check (tier in ('small','medium','large','premium')),
  display_diamonds integer not null default 0 check (display_diamonds between 0 and 50),       -- animation only
  diamond_display_value integer not null default 0 check (diamond_display_value >= 0),         -- stated value of ONE displayed diamond
  icon text not null,
  color text,
  sort_order integer not null default 0,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  -- when diamonds are shown, their stated total must equal the gift's price: it is a label, never extra money
  check (display_diamonds * diamond_display_value in (0, price))
);
insert into live_gifts(code, name, price, tier, display_diamonds, diamond_display_value, icon, color, sort_order) values
  ('red_rose',       'Red Rose',       10,    'small',   0,  0,   'rose',     '#ff4d6d', 10),
  ('love_heart',     'Love Heart',     50,    'small',   0,  0,   'heart',    '#ff3d81', 20),
  ('mystery_box',    'Mystery Box',    100,   'medium',  0,  0,   'box',      '#8b5cf6', 30),
  ('golden_crown',   'Golden Crown',   500,   'medium',  0,  0,   'crown',    '#f7b500', 40),
  ('treasure_chest', 'Treasure Chest', 1000,  'large',   10, 100, 'chest',    '#d99a00', 50),
  ('sports_car',     'Sports Car',     2000,  'large',   0,  0,   'car',      '#2f6bff', 60),
  ('diamond_ring',   'Diamond Ring',   5000,  'premium', 0,  0,   'ring',     '#5eead4', 70),
  ('private_jet',    'Private Jet',    10000, 'premium', 0,  0,   'jet',      '#e5e7eb', 80)
on conflict (code) do nothing;

-- ---------- sessions ----------
create table if not exists live_sessions (
  id uuid primary key default gen_random_uuid(),
  host_id uuid not null references profiles(id) on delete cascade,
  title text not null check (char_length(title) between 1 and 80),
  category text not null default 'chat' check (char_length(category) <= 24),
  mode text not null default 'video' check (mode in ('video','audio')),
  visibility text not null default 'public' check (visibility in ('public','followers')),
  status text not null default 'live' check (status in ('live','ended')),
  room_name text not null unique,                       -- the media-provider room; never shown to other users
  chat_enabled boolean not null default true,
  slow_mode_seconds integer not null default 0 check (slow_mode_seconds between 0 and 120),
  guests_enabled boolean not null default true,
  pinned_event_id bigint,
  started_at timestamptz not null default now(),
  ended_at timestamptz,
  end_reason text,
  last_heartbeat_at timestamptz not null default now(),
  peak_viewers integer not null default 0,
  gifts_count integer not null default 0,
  gift_gross integer not null default 0,               -- coins viewers spent
  host_earned integer not null default 0,              -- coins credited to the host (the real earnings)
  platform_share integer not null default 0,
  diamonds_displayed integer not null default 0,       -- animation count only; NOT money
  check ((status = 'live') = (ended_at is null))
);
create unique index if not exists live_sessions_one_live_per_host on live_sessions(host_id) where status = 'live';
create index if not exists live_sessions_status_idx on live_sessions(status, started_at desc);
create index if not exists live_sessions_host_idx on live_sessions(host_id, started_at desc);

create table if not exists live_viewers (
  session_id uuid not null references live_sessions(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  role text not null default 'viewer' check (role in ('viewer','moderator')),
  joined_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  left_at timestamptz,
  muted_until timestamptz,
  removed boolean not null default false,
  primary key (session_id, user_id)
);
create index if not exists live_viewers_seen_idx on live_viewers(session_id, last_seen_at desc);

-- The realtime feed. kind: chat | gift | join | pin | unpin | delete_msg | mod | state | reaction | guest | battle_* | ended
-- Clients poll "id > last seen". Chat messages are rows with kind = 'chat' (persisted, moderatable, deletable).
create table if not exists live_events (
  id bigint generated always as identity primary key,
  session_id uuid not null references live_sessions(id) on delete cascade,
  kind text not null,
  actor_id uuid references profiles(id) on delete set null,
  body text check (char_length(body) <= 300),
  payload jsonb not null default '{}'::jsonb,
  deleted_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists live_events_session_idx on live_events(session_id, id);
create index if not exists live_events_actor_idx on live_events(session_id, actor_id, created_at desc);

create table if not exists live_host_blocks (          -- a host's permanent "never let this person into my lives"
  host_id uuid not null references profiles(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (host_id, user_id), check (host_id <> user_id)
);

create table if not exists live_moderation_actions (   -- append-only
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references live_sessions(id) on delete cascade,
  actor_id uuid references profiles(id) on delete set null,
  target_id uuid references profiles(id) on delete set null,
  action text not null check (action in ('mute','unmute','timeout','block','unblock','remove','delete_message','pin','unpin',
                                         'mod_add','mod_remove','slow_mode','guest_remove','guest_mute','end_session')),
  reason text check (char_length(reason) <= 200),
  expires_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists live_mod_actions_session_idx on live_moderation_actions(session_id, created_at desc);

create table if not exists live_reports (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references live_sessions(id) on delete cascade,
  reporter_id uuid not null references profiles(id) on delete cascade,
  target_user_id uuid references profiles(id) on delete set null,
  event_id bigint,
  reason text not null check (reason in ('harassment','spam','inappropriate','unsafe','impersonation','other')),
  details text check (char_length(details) <= 500),
  status text not null default 'open' check (status in ('open','actioned','dismissed')),
  resolved_by uuid references profiles(id) on delete set null,
  resolved_at timestamptz,
  created_at timestamptz not null default now()
);
create unique index if not exists live_reports_once on live_reports(reporter_id, session_id, coalesce(target_user_id, '00000000-0000-0000-0000-000000000000'::uuid), coalesce(event_id, 0));
create index if not exists live_reports_status_idx on live_reports(status, created_at desc);

-- ---------- guests (max enforced by cirq_live_guest_accept) ----------
create table if not exists live_guest_invites (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references live_sessions(id) on delete cascade,
  guest_id uuid not null references profiles(id) on delete cascade,
  initiated_by text not null check (initiated_by in ('host','guest')),
  status text not null default 'invited' check (status in ('invited','requested','accepted','rejected','removed','left')),
  media text not null default 'video' check (media in ('video','audio')),
  can_publish_audio boolean not null default true,
  can_publish_video boolean not null default true,
  consent_at timestamptz,                               -- the guest agreed to share their media; required before they can publish
  created_at timestamptz not null default now(),
  accepted_at timestamptz,
  ended_at timestamptz,
  unique (session_id, guest_id),
  check (status <> 'accepted' or consent_at is not null)
);
create index if not exists live_guest_invites_session_idx on live_guest_invites(session_id, status);

-- ---------- gifts ----------
create table if not exists live_gift_transactions (
  id uuid primary key default gen_random_uuid(),
  request_id text not null check (char_length(request_id) between 8 and 80),   -- idempotency key made by the browser per tap
  session_id uuid not null references live_sessions(id) on delete restrict,
  sender_id uuid not null references profiles(id) on delete restrict,
  host_id uuid not null references profiles(id) on delete restrict,
  gift_code text not null references live_gifts(code),
  gross_coins integer not null check (gross_coins > 0),            -- what the viewer paid
  host_coins integer not null check (host_coins >= 0),             -- what the host was credited
  platform_coins integer not null check (platform_coins >= 0),     -- the platform's share (not credited to anyone)
  displayed_diamonds integer not null default 0 check (displayed_diamonds >= 0),
  displayed_diamond_value integer not null default 0 check (displayed_diamond_value >= 0),  -- stated value of one diamond (label only)
  battle_id uuid,
  created_at timestamptz not null default now(),
  unique (sender_id, request_id),
  check (host_coins + platform_coins = gross_coins),
  check (sender_id <> host_id)
);
create index if not exists live_gift_tx_session_idx on live_gift_transactions(session_id, created_at desc);
create index if not exists live_gift_tx_host_idx on live_gift_transactions(host_id, created_at desc);
create index if not exists live_gift_tx_sender_idx on live_gift_transactions(sender_id, created_at desc);

-- One row per diamond SHOWN by an animation. These are never added to earnings (counts_toward_earnings is always false).
create table if not exists live_diamond_records (
  id uuid primary key default gen_random_uuid(),
  transaction_id uuid not null references live_gift_transactions(id) on delete cascade,
  host_id uuid not null references profiles(id) on delete cascade,
  seq integer not null check (seq >= 1),
  display_value integer not null check (display_value >= 0),
  counts_toward_earnings boolean not null default false check (counts_toward_earnings = false),
  created_at timestamptz not null default now(),
  unique (transaction_id, seq)
);
create index if not exists live_diamonds_host_idx on live_diamond_records(host_id);

-- The host's real earnings, one row per gift; transaction_id is unique so a gift can never be credited twice.
create table if not exists live_creator_earnings (
  id uuid primary key default gen_random_uuid(),
  host_id uuid not null references profiles(id) on delete cascade,
  session_id uuid not null references live_sessions(id) on delete restrict,
  transaction_id uuid not null unique references live_gift_transactions(id) on delete cascade,
  coins integer not null check (coins >= 0),
  ledger_ref text not null,                              -- coin_ledger.ref of the credit (reason 'live_gift_in')
  created_at timestamptz not null default now()
);
create index if not exists live_earnings_host_idx on live_creator_earnings(host_id, created_at desc);

create table if not exists live_supporters (            -- lifetime total a viewer has gifted a host: drives supporter badges
  host_id uuid not null references profiles(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  total_coins integer not null default 0 check (total_coins >= 0),
  gifts_count integer not null default 0,
  last_gift_at timestamptz not null default now(),
  primary key (host_id, user_id)
);
create index if not exists live_supporters_rank_idx on live_supporters(host_id, total_coins desc);

create table if not exists live_fan_club_members (      -- a viewer joins a host's fan club by sending their first gift; they can leave any time
  host_id uuid not null references profiles(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  joined_at timestamptz not null default now(),
  primary key (host_id, user_id)
);

-- ---------- PK battles ----------
create table if not exists live_battles (
  id uuid primary key default gen_random_uuid(),
  session_a uuid not null references live_sessions(id) on delete cascade,   -- the inviting host's session
  session_b uuid not null references live_sessions(id) on delete cascade,
  host_a uuid not null references profiles(id) on delete cascade,
  host_b uuid not null references profiles(id) on delete cascade,
  status text not null default 'invited' check (status in ('invited','countdown','active','ended','declined','cancelled','expired')),
  duration_seconds integer not null check (duration_seconds between 60 and 1800),
  countdown_seconds integer not null default 5 check (countdown_seconds between 0 and 30),
  invited_at timestamptz not null default now(),
  expires_at timestamptz not null,
  accepted_at timestamptz,
  starts_at timestamptz,
  ends_at timestamptz,
  ended_at timestamptz,
  outcome text check (outcome in ('a','b','draw')),
  winner_host_id uuid references profiles(id),
  check (host_a <> host_b)
);
create index if not exists live_battles_hosts_idx on live_battles(host_a, host_b, invited_at desc);

create table if not exists live_battle_participants (
  battle_id uuid not null references live_battles(id) on delete cascade,
  host_id uuid not null references profiles(id) on delete cascade,
  session_id uuid not null references live_sessions(id) on delete cascade,
  side text not null check (side in ('a','b')),
  score integer not null default 0 check (score >= 0),
  active boolean not null default false,
  primary key (battle_id, host_id)
);
-- A host can be in at most one running battle: this index is the guard against invalid/overlapping battle states.
create unique index if not exists live_battle_one_active_per_host on live_battle_participants(host_id) where active;
create index if not exists live_battle_participants_session_idx on live_battle_participants(session_id);

-- =====================================================================================
-- FUNCTIONS (all security definer; callable only by the server's service role)
-- =====================================================================================

-- Battle clock. Idempotent: moves invited->expired, countdown->active, active->ended at the right server time and writes
-- the matching feed events exactly once. p_force ends a running battle now. Returns the battle's status afterwards.
create or replace function cirq_live_battle_tick(p_battle uuid, p_force boolean default false)
returns text language plpgsql security definer set search_path = public as $$
declare b live_battles%rowtype; sa integer; sb integer; outc text; win uuid; scores jsonb;
begin
  select * into b from live_battles where id = p_battle for update;
  if not found then return 'missing'; end if;
  if b.status = 'invited' and (b.expires_at <= now() or p_force) then
    update live_battles set status = case when p_force then 'cancelled' else 'expired' end, ended_at = now() where id = b.id;
    return case when p_force then 'cancelled' else 'expired' end;
  end if;
  if b.status = 'countdown' and p_force then
    update live_battle_participants set active = false where battle_id = b.id;
    update live_battles set status = 'cancelled', ended_at = now() where id = b.id;
    insert into live_events(session_id, kind, payload) select s, 'battle_end', jsonb_build_object('battle_id', b.id, 'outcome', 'cancelled')
      from unnest(array[b.session_a, b.session_b]) s;
    return 'cancelled';
  end if;
  if b.status = 'countdown' and now() >= b.starts_at then
    update live_battles set status = 'active' where id = b.id;
    b.status := 'active';
    insert into live_events(session_id, kind, payload) select s, 'battle_start',
      jsonb_build_object('battle_id', b.id, 'starts_at', b.starts_at, 'ends_at', b.ends_at) from unnest(array[b.session_a, b.session_b]) s;
  end if;
  if b.status = 'active' and (p_force or now() >= b.ends_at) then
    select score into sa from live_battle_participants where battle_id = b.id and host_id = b.host_a;
    select score into sb from live_battle_participants where battle_id = b.id and host_id = b.host_b;
    outc := case when sa > sb then 'a' when sb > sa then 'b' else 'draw' end;
    win := case outc when 'a' then b.host_a when 'b' then b.host_b else null end;
    update live_battle_participants set active = false where battle_id = b.id;
    update live_battles set status = 'ended', ended_at = now(), outcome = outc, winner_host_id = win where id = b.id;
    scores := jsonb_build_object('a', sa, 'b', sb);
    insert into live_events(session_id, kind, payload) select s, 'battle_end',
      jsonb_build_object('battle_id', b.id, 'outcome', outc, 'winner_host_id', win, 'scores', scores) from unnest(array[b.session_a, b.session_b]) s;
    return 'ended';
  end if;
  return b.status;
end $$;

-- The invited host accepts: both hosts must be live and free. Returns ok | forbidden | bad_state | expired | not_live | busy | missing
create or replace function cirq_live_battle_accept(p_battle uuid, p_actor uuid, p_countdown integer default 5)
returns text language plpgsql security definer set search_path = public as $$
declare b live_battles%rowtype; live_count integer; st timestamptz;
begin
  select * into b from live_battles where id = p_battle for update;
  if not found then return 'missing'; end if;
  if b.host_b <> p_actor then return 'forbidden'; end if;
  if b.status <> 'invited' then return 'bad_state'; end if;
  if b.expires_at <= now() then update live_battles set status = 'expired', ended_at = now() where id = b.id; return 'expired'; end if;
  select count(*) into live_count from live_sessions where id in (b.session_a, b.session_b) and status = 'live';
  if live_count <> 2 then return 'not_live'; end if;
  begin
    update live_battle_participants set active = true where battle_id = b.id;
  exception when unique_violation then
    return 'busy';
  end;
  st := now() + make_interval(secs => greatest(0, p_countdown));
  update live_battles set status = 'countdown', accepted_at = now(), countdown_seconds = greatest(0, p_countdown),
         starts_at = st, ends_at = st + make_interval(secs => b.duration_seconds) where id = b.id;
  insert into live_events(session_id, kind, payload) select s, 'battle_accept',
    jsonb_build_object('battle_id', b.id, 'starts_at', st, 'ends_at', st + make_interval(secs => b.duration_seconds)) from unnest(array[b.session_a, b.session_b]) s;
  return 'ok';
end $$;

-- Ends a session once (true) or reports it was already over (false). Also settles battles, guests and viewers.
create or replace function cirq_live_end_session(p_session uuid, p_reason text default 'host_ended')
returns boolean language plpgsql security definer set search_path = public as $$
declare s live_sessions%rowtype; r record;
begin
  select * into s from live_sessions where id = p_session for update;
  if not found or s.status <> 'live' then return false; end if;
  update live_sessions set status = 'ended', ended_at = now(), end_reason = left(coalesce(p_reason, 'host_ended'), 60), pinned_event_id = null where id = p_session;
  for r in select distinct battle_id from live_battle_participants where session_id = p_session loop
    perform cirq_live_battle_tick(r.battle_id, true);
  end loop;
  update live_guest_invites set status = case when status = 'accepted' then 'left' else 'removed' end, ended_at = now()
    where session_id = p_session and status in ('invited','requested','accepted');
  update live_viewers set left_at = coalesce(left_at, now()) where session_id = p_session and left_at is null;
  insert into live_events(session_id, kind, payload) values (p_session, 'ended', jsonb_build_object('reason', coalesce(p_reason, 'host_ended')));
  return true;
end $$;

-- Guest becomes "accepted" under the session lock so the limit of p_max guests cannot be exceeded by two clicks at once.
-- Guest accepting a host's invitation records their consent. Returns ok | full | forbidden | bad_state | ended | missing
create or replace function cirq_live_guest_accept(p_invite uuid, p_actor uuid, p_max integer default 4)
returns text language plpgsql security definer set search_path = public as $$
declare i live_guest_invites%rowtype; s live_sessions%rowtype; n integer;
begin
  select session_id into i.session_id from live_guest_invites where id = p_invite;
  if not found then return 'missing'; end if;
  select * into s from live_sessions where id = i.session_id for update;
  select * into i from live_guest_invites where id = p_invite for update;
  if s.status <> 'live' then return 'ended'; end if;
  if not ((i.status = 'invited' and p_actor = i.guest_id) or (i.status = 'requested' and p_actor = s.host_id)) then
    if i.status in ('invited','requested') then return 'forbidden'; end if;
    return 'bad_state';
  end if;
  select count(*) into n from live_guest_invites where session_id = s.id and status = 'accepted';
  if n >= p_max then return 'full'; end if;
  update live_guest_invites set status = 'accepted', accepted_at = now(), consent_at = coalesce(consent_at, now()) where id = i.id;
  return 'ok';
end $$;

-- THE gift function. One transaction: validate, charge the viewer, credit the host, record the gift, the displayed diamonds,
-- the creator earning, supporter/fan-club rows, battle score and the feed event. Replays of the same (sender, request_id)
-- return the original result without charging again.
-- status: ok | duplicate | insufficient | session_ended | self_gift | gift_unavailable | cap_reached | blocked | bad_request | request_reuse
create or replace function cirq_live_send_gift(p_sender uuid, p_session uuid, p_gift text, p_request text, p_daily_cap integer default 0)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  v_host uuid; s live_sessions%rowtype; g live_gifts%rowtype; t live_gift_transactions%rowtype;
  v_host_coins integer; v_platform integer; v_balance integer; v_spent integer;
  v_battle uuid; v_bstat text; v_scores jsonb; v_sname text; v_sdisp text; v_txn uuid; i integer;
begin
  if p_request is null or char_length(p_request) < 8 or char_length(p_request) > 80 then return jsonb_build_object('status', 'bad_request'); end if;
  select host_id into v_host from live_sessions where id = p_session;
  if v_host is null then return jsonb_build_object('status', 'session_ended'); end if;
  if v_host = p_sender then return jsonb_build_object('status', 'self_gift'); end if;

  -- lock both wallets in a fixed order so concurrent gifts cannot deadlock
  if p_sender < v_host then
    perform 1 from profiles where id = p_sender for update; perform 1 from profiles where id = v_host for update;
  else
    perform 1 from profiles where id = v_host for update; perform 1 from profiles where id = p_sender for update;
  end if;

  select * into t from live_gift_transactions where sender_id = p_sender and request_id = p_request;
  if found then
    if t.session_id <> p_session or t.gift_code <> p_gift then return jsonb_build_object('status', 'request_reuse'); end if;
    select coin_balance into v_balance from profiles where id = p_sender;
    return jsonb_build_object('status', 'duplicate', 'txn_id', t.id, 'gift_code', t.gift_code, 'gross', t.gross_coins,
      'host_coins', t.host_coins, 'platform_coins', t.platform_coins, 'diamonds', t.displayed_diamonds,
      'diamond_value', t.displayed_diamond_value, 'balance', v_balance, 'host_id', t.host_id, 'battle_id', t.battle_id);
  end if;

  select * into s from live_sessions where id = p_session for update;
  if s.status <> 'live' then return jsonb_build_object('status', 'session_ended'); end if;
  if exists (select 1 from live_host_blocks where host_id = v_host and user_id = p_sender)
     or exists (select 1 from live_viewers where session_id = p_session and user_id = p_sender and removed)
     or exists (select 1 from blocks where (blocker_id = v_host and blocked_id = p_sender) or (blocker_id = p_sender and blocked_id = v_host)) then
    return jsonb_build_object('status', 'blocked');
  end if;
  select * into g from live_gifts where code = p_gift and active;
  if not found then return jsonb_build_object('status', 'gift_unavailable'); end if;

  select coin_balance into v_balance from profiles where id = p_sender;
  if v_balance < g.price then return jsonb_build_object('status', 'insufficient', 'balance', v_balance, 'price', g.price); end if;
  if p_daily_cap > 0 then
    select coalesce(sum(gross_coins), 0) into v_spent from live_gift_transactions where sender_id = p_sender and created_at > now() - interval '24 hours';
    if v_spent + g.price > p_daily_cap then return jsonb_build_object('status', 'cap_reached', 'cap', p_daily_cap, 'spent', v_spent); end if;
  end if;

  v_host_coins := (g.price * g.host_share_pct) / 100;
  v_platform := g.price - v_host_coins;
  v_txn := gen_random_uuid();

  -- battle: count this gift only while the host's battle is genuinely running (server clock)
  select p.battle_id into v_battle from live_battle_participants p where p.host_id = v_host and p.active limit 1;
  if v_battle is not null then
    v_bstat := cirq_live_battle_tick(v_battle);
    if v_bstat <> 'active' then v_battle := null; end if;
  end if;

  insert into live_gift_transactions(id, request_id, session_id, sender_id, host_id, gift_code, gross_coins, host_coins, platform_coins,
                                     displayed_diamonds, displayed_diamond_value, battle_id)
    values (v_txn, p_request, p_session, p_sender, v_host, g.code, g.price, v_host_coins, v_platform, g.display_diamonds, g.diamond_display_value, v_battle);

  insert into coin_ledger(user_id, amount, reason, ref) values (p_sender, -g.price, 'live_gift_out', v_txn::text);
  update profiles set coin_balance = coin_balance - g.price where id = p_sender returning coin_balance into v_balance;
  if v_host_coins > 0 then
    insert into coin_ledger(user_id, amount, reason, ref) values (v_host, v_host_coins, 'live_gift_in', v_txn::text);
    update profiles set coin_balance = coin_balance + v_host_coins where id = v_host;
  end if;
  insert into live_creator_earnings(host_id, session_id, transaction_id, coins, ledger_ref) values (v_host, p_session, v_txn, v_host_coins, v_txn::text);

  for i in 1..g.display_diamonds loop
    insert into live_diamond_records(transaction_id, host_id, seq, display_value) values (v_txn, v_host, i, g.diamond_display_value);
  end loop;

  insert into live_supporters(host_id, user_id, total_coins, gifts_count) values (v_host, p_sender, g.price, 1)
    on conflict (host_id, user_id) do update set total_coins = live_supporters.total_coins + excluded.total_coins,
      gifts_count = live_supporters.gifts_count + 1, last_gift_at = now();
  insert into live_fan_club_members(host_id, user_id) values (v_host, p_sender) on conflict do nothing;

  update live_sessions set gifts_count = gifts_count + 1, gift_gross = gift_gross + g.price, host_earned = host_earned + v_host_coins,
         platform_share = platform_share + v_platform, diamonds_displayed = diamonds_displayed + g.display_diamonds where id = p_session;

  if v_battle is not null then
    update live_battle_participants set score = score + g.price where battle_id = v_battle and host_id = v_host;
    select jsonb_object_agg(side, score) into v_scores from live_battle_participants where battle_id = v_battle;
  end if;

  select username, display_name into v_sname, v_sdisp from profiles where id = p_sender;
  insert into live_events(session_id, kind, actor_id, payload) values (p_session, 'gift', p_sender, jsonb_build_object(
    'txn_id', v_txn, 'gift_code', g.code, 'name', g.name, 'icon', g.icon, 'tier', g.tier, 'gross', g.price, 'host_coins', v_host_coins,
    'diamonds', g.display_diamonds, 'diamond_value', g.diamond_display_value, 'sender', v_sname, 'sender_name', v_sdisp,
    'host_id', v_host, 'battle_id', v_battle, 'scores', v_scores));

  return jsonb_build_object('status', 'ok', 'txn_id', v_txn, 'gift_code', g.code, 'gross', g.price, 'host_coins', v_host_coins,
    'platform_coins', v_platform, 'diamonds', g.display_diamonds, 'diamond_value', g.diamond_display_value, 'balance', v_balance,
    'host_id', v_host, 'battle_id', v_battle, 'scores', v_scores);
end $$;

-- Creator dashboard totals in one round trip.
create or replace function cirq_live_creator_summary(p_host uuid)
returns jsonb language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'gifts_received', count(*),
    'gross_value', coalesce(sum(gross_coins), 0),
    'host_earnings', coalesce(sum(host_coins), 0),
    'platform_share', coalesce(sum(platform_coins), 0),
    'diamonds_displayed', coalesce(sum(displayed_diamonds), 0),
    'sessions', (select count(*) from live_sessions where host_id = p_host))
  from live_gift_transactions where host_id = p_host;
$$;

create or replace function cirq_live_top_gifters(p_host uuid, p_since timestamptz, p_limit integer default 20)
returns table(user_id uuid, total_coins bigint, gifts bigint) language sql stable security definer set search_path = public as $$
  select sender_id, sum(gross_coins)::bigint, count(*)::bigint from live_gift_transactions
   where (p_host is null or host_id = p_host) and (p_since is null or created_at >= p_since)
   group by sender_id order by 2 desc, 3 desc limit greatest(1, least(p_limit, 100));
$$;

create or replace function cirq_live_host_ranking(p_since timestamptz, p_limit integer default 20)
returns table(host_id uuid, host_coins bigint, gifts bigint) language sql stable security definer set search_path = public as $$
  select t.host_id, sum(t.host_coins)::bigint, count(*)::bigint from live_gift_transactions t
   where p_since is null or t.created_at >= p_since
   group by t.host_id order by 2 desc, 3 desc limit greatest(1, least(p_limit, 100));
$$;

revoke all on function cirq_live_battle_tick(uuid, boolean) from public, anon, authenticated;
revoke all on function cirq_live_battle_accept(uuid, uuid, integer) from public, anon, authenticated;
revoke all on function cirq_live_end_session(uuid, text) from public, anon, authenticated;
revoke all on function cirq_live_guest_accept(uuid, uuid, integer) from public, anon, authenticated;
revoke all on function cirq_live_send_gift(uuid, uuid, text, text, integer) from public, anon, authenticated;
revoke all on function cirq_live_creator_summary(uuid) from public, anon, authenticated;
revoke all on function cirq_live_top_gifters(uuid, timestamptz, integer) from public, anon, authenticated;
revoke all on function cirq_live_host_ranking(timestamptz, integer) from public, anon, authenticated;

-- ---------- Row Level Security: everything denied to the browser's own key; only the gift catalogue is readable ----------
do $$
declare t text;
begin
  for t in select unnest(array['live_gifts','live_sessions','live_viewers','live_events','live_host_blocks','live_moderation_actions','live_reports',
    'live_guest_invites','live_gift_transactions','live_diamond_records','live_creator_earnings','live_supporters','live_fan_club_members',
    'live_battles','live_battle_participants'])
  loop execute format('alter table %I enable row level security', t); end loop;
end $$;
drop policy if exists live_gifts_read on live_gifts;
create policy live_gifts_read on live_gifts for select to authenticated using (active);

-- Make PostgREST pick up the new tables and columns right away.
notify pgrst, 'reload schema';
