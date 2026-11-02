-- Unique, qualified reel views with a cached counter. Run in Supabase SQL Editor.

alter table public.posts
  add column if not exists views_count integer not null default 0;
alter table public.posts
  drop constraint if exists posts_views_count_check;
alter table public.posts
  add constraint posts_views_count_check check (views_count >= 0);

create table if not exists public.reel_views (
  id uuid primary key default gen_random_uuid(),
  reel_id uuid not null references public.posts(id) on delete cascade,
  viewer_id uuid not null references public.profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  constraint reel_views_reel_viewer_unique unique (reel_id, viewer_id)
);
create index if not exists reel_views_reel_idx on public.reel_views(reel_id);
create index if not exists reel_views_viewer_idx on public.reel_views(viewer_id);

update public.posts p
set views_count = (
  select count(*)::integer
  from public.reel_views rv
  where rv.reel_id = p.id
)
where p.kind = 'reel';

create or replace function public.cirq_bump_reel_views_count()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if tg_op = 'INSERT' then
    update public.posts
      set views_count = views_count + 1
      where id = new.reel_id and kind = 'reel';
    return new;
  end if;

  update public.posts
    set views_count = greatest(views_count - 1, 0)
    where id = old.reel_id and kind = 'reel';
  return old;
end;
$$;
revoke all on function public.cirq_bump_reel_views_count() from public;

drop trigger if exists reel_views_counter on public.reel_views;
create trigger reel_views_counter
  after insert or delete on public.reel_views
  for each row execute function public.cirq_bump_reel_views_count();

-- Used by the insert policy to check the author's block state and visibility despite
-- the caller's row-level restrictions on blocks and profiles.
create or replace function public.cirq_can_record_reel_view(p_reel_id uuid, p_viewer_id uuid)
returns boolean
language sql
security definer
stable
set search_path = ''
as $$
  select p_viewer_id = auth.uid() and exists (
    select 1
    from public.posts p
    join public.profiles author on author.id = p.author_id
    where p.id = p_reel_id
      and p.kind = 'reel'
      and p.moderation_status = 'ok'
      and p.community_id is null
      and p.author_id <> p_viewer_id
      and ((p.visibility = 'public' and not author.is_private)
        or exists (
          select 1 from public.follows f
          where f.follower_id = p_viewer_id and f.followee_id = p.author_id
        ))
      and not exists (
        select 1 from public.blocks b
        where (b.blocker_id = p_viewer_id and b.blocked_id = p.author_id)
           or (b.blocker_id = p.author_id and b.blocked_id = p_viewer_id)
      )
  );
$$;
revoke all on function public.cirq_can_record_reel_view(uuid, uuid) from public;
grant execute on function public.cirq_can_record_reel_view(uuid, uuid) to authenticated;

alter table public.reel_views enable row level security;
drop policy if exists reel_views_insert_own on public.reel_views;
create policy reel_views_insert_own on public.reel_views
  for insert to authenticated
  with check (
    viewer_id = auth.uid()
    and public.cirq_can_record_reel_view(reel_id, viewer_id)
  );
-- Intentionally no SELECT policy: clients read the cached count on posts, not viewer rows.

-- The Flask route checks eligibility, then calls this single atomic operation. The
-- unique key and trigger make both duplicate requests and concurrent requests safe.
create or replace function public.cirq_record_reel_view(p_reel_id uuid, p_viewer_id uuid)
returns table(counted boolean, views_count integer)
language plpgsql
security definer
set search_path = ''
as $$
declare
  inserted_count integer;
begin
  insert into public.reel_views (reel_id, viewer_id)
  select p.id, p_viewer_id
  from public.posts p
  where p.id = p_reel_id
    and p.kind = 'reel'
    and p.author_id <> p_viewer_id
  on conflict (reel_id, viewer_id) do nothing;
  get diagnostics inserted_count = row_count;

  return query
    select inserted_count = 1, p.views_count
    from public.posts p
    where p.id = p_reel_id and p.kind = 'reel';
end;
$$;
revoke all on function public.cirq_record_reel_view(uuid, uuid) from public, anon, authenticated;
grant execute on function public.cirq_record_reel_view(uuid, uuid) to service_role;

notify pgrst, 'reload schema';
