-- Fixes: GET /linkup -> 500 "column conversations.expires_at does not exist" (event_chat.expire_due()).
-- Additive and safe to run more than once. Existing conversations get NULL (= never expires).
alter table conversations add column if not exists expires_at timestamptz;
create index if not exists conversations_expires_at_idx on conversations (expires_at) where expires_at is not null;
notify pgrst, 'reload schema';
