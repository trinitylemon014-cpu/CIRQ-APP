-- Replies to comments. Run in the Supabase SQL Editor.
-- Replies are one level deep: parent_id is always the top-level comment of the thread.
-- reply_to_user_id is the person being answered (so "Replying to @name" works even when answering another reply).

alter table public.comments
  add column if not exists parent_id uuid references public.comments(id) on delete cascade,
  add column if not exists reply_to_user_id uuid references public.profiles(id) on delete set null,
  add column if not exists reply_count integer not null default 0;

create index if not exists comments_parent_idx
  on public.comments(parent_id, created_at, id) where parent_id is not null;
-- The post's comment list only needs top-level comments:
create index if not exists comments_top_idx
  on public.comments(post_id, created_at desc, id desc) where parent_id is null;

notify pgrst, 'reload schema';
