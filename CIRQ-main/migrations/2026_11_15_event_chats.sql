-- Event group chats close themselves once the event date has passed.
-- Run in the Supabase SQL Editor.

alter table public.conversations
  add column if not exists expires_at timestamptz;          -- null = never (normal chats); set for event chats
create index if not exists conversations_expires_idx
  on public.conversations(expires_at) where expires_at is not null;

-- Existing event chats: they close at the end of the event's day (East Africa Time), or when the event ends if later.
-- Chats for events that are already over will be removed the next time the cleanup runs.
update public.conversations c
set expires_at = greatest(
      coalesce(e.ends_at, e.starts_at),
      ((date_trunc('day', e.starts_at at time zone 'Africa/Nairobi') + interval '1 day') at time zone 'Africa/Nairobi'))
from public.events e
where c.event_id = e.id and c.expires_at is null;

notify pgrst, 'reload schema';
