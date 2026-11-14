-- Chat: delivery ticks (sent / delivered / seen) and per-user disappearing messages.
-- Run in the Supabase SQL Editor.

-- 1) Delivery ticks. "Seen" already comes from conversation_members.last_read_at.
alter table public.messages
  add column if not exists delivered_at timestamptz;

-- 2) Disappearing messages, chosen by each person for themselves (seconds; null = off).
alter table public.conversation_members
  add column if not exists disappear_after integer;
alter table public.conversation_members
  drop constraint if exists conversation_members_disappear_after_check;
alter table public.conversation_members
  add constraint conversation_members_disappear_after_check
  check (disappear_after is null or disappear_after > 0);

-- One row per (message, person who has a timer on): when that message disappears for them.
create table if not exists public.message_hides (
  id uuid primary key default gen_random_uuid(),
  message_id uuid not null references public.messages(id) on delete cascade,
  user_id uuid not null references public.profiles(id) on delete cascade,
  conversation_id uuid not null references public.conversations(id) on delete cascade,
  hide_at timestamptz not null,
  constraint message_hides_message_user_unique unique (message_id, user_id)
);
create index if not exists message_hides_user_idx on public.message_hides(user_id, hide_at);
create index if not exists message_hides_conv_idx on public.message_hides(conversation_id, hide_at);
alter table public.message_hides enable row level security;   -- server (service role) only

notify pgrst, 'reload schema';
