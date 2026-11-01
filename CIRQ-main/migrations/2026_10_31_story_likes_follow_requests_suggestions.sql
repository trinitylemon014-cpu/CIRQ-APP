-- =====================================================================================
-- CIRQ migration · 31 Oct 2026
-- Story likes + story replies in the inbox, private accounts with follow requests, suggested posts.
--
-- WHO NEEDS THIS: anyone whose Supabase project was created from an OLDER schema.sql.
--   * Brand-new project?  Skip this file and just run schema.sql (it already contains everything below).
--   * Existing project?   Run this whole file once: Supabase Dashboard -> SQL Editor -> New query -> Run.
-- It is idempotent (IF NOT EXISTS / DROP POLICY IF EXISTS), so running it twice is harmless.
-- =====================================================================================

-- ---------- 1. Follow requests (private accounts) ----------
create table if not exists follow_requests (
  requester_id uuid not null references profiles(id) on delete cascade,
  target_id uuid not null references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (requester_id, target_id),
  check (requester_id <> target_id)
);
create index if not exists follow_requests_target_idx on follow_requests(target_id, created_at desc);

-- ---------- 2. Story likes ----------
create table if not exists story_likes (
  story_id uuid not null references stories(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (story_id, user_id)
);
create index if not exists story_likes_story_idx on story_likes(story_id, created_at desc);

-- ---------- 3. Story replies arrive as direct messages ----------
alter table messages add column if not exists story_id uuid references stories(id) on delete set null;
alter table messages add column if not exists story_reply boolean not null default false;

-- ---------- 4. Personalised suggestions (recommender) ----------
-- "Not interested" is stored as a 'hide' event in post_signals, so no separate table is needed.
alter table profiles add column if not exists personalized_recs boolean not null default true;

create table if not exists post_signals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id) on delete cascade,
  post_id uuid not null references posts(id) on delete cascade,
  author_id uuid not null references profiles(id) on delete cascade,
  event text not null check (event in ('impression','view','watch','like','comment','share','hide')),
  value numeric not null default 0,
  created_at timestamptz not null default now()
);
create index if not exists post_signals_user_idx on post_signals(user_id, created_at desc);
create index if not exists post_signals_user_event_idx on post_signals(user_id, event, created_at desc);

create table if not exists user_interests (
  user_id uuid not null references profiles(id) on delete cascade,
  dim text not null check (dim in ('author','topic','school','kind')),
  key text not null,
  weight double precision not null default 0,
  updated_at timestamptz not null default now(),
  primary key (user_id, dim, key)
);
create index if not exists user_interests_user_idx on user_interests(user_id, updated_at desc);

-- ---------- 5. Row Level Security for the new tables ----------
alter table follow_requests enable row level security;
alter table story_likes enable row level security;
alter table post_signals enable row level security;      -- no policies: server (service role) only
alter table user_interests enable row level security;

drop policy if exists freq_read on follow_requests;
create policy freq_read on follow_requests for select to authenticated using (requester_id = auth.uid() or target_id = auth.uid());
drop policy if exists freq_insert on follow_requests;
create policy freq_insert on follow_requests for insert to authenticated with check (requester_id = auth.uid());
drop policy if exists freq_delete on follow_requests;
create policy freq_delete on follow_requests for delete to authenticated using (requester_id = auth.uid() or target_id = auth.uid());

drop policy if exists story_likes_own on story_likes;
create policy story_likes_own on story_likes for all to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());
drop policy if exists story_likes_owner_read on story_likes;
create policy story_likes_owner_read on story_likes for select to authenticated
  using (exists (select 1 from stories s where s.id = story_id and s.author_id = auth.uid()));
