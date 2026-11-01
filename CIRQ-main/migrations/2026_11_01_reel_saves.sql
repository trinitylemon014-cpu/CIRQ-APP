-- Private per-user bookmarks used by the reel viewer. Safe to run more than once.
create index if not exists posts_reel_stream_idx on posts(created_at desc, id desc)
  where kind = 'reel' and moderation_status = 'ok' and community_id is null;

create table if not exists saved_posts (
  post_id uuid not null references posts(id) on delete cascade,
  user_id uuid not null references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (post_id, user_id)
);
create index if not exists saved_posts_user_idx on saved_posts(user_id, created_at desc);

alter table saved_posts enable row level security;
drop policy if exists saved_posts_own on saved_posts;
create policy saved_posts_own on saved_posts for all to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());

notify pgrst, 'reload schema';
