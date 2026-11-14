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

-- Make PostgREST (Supabase's API) see the new tables and functions right away.
notify pgrst, 'reload schema';
