-- Chat attachments use a private Supabase Storage bucket. Run this migration in the
-- Supabase SQL Editor, then follow the bucket setup notes in README.md.

alter table messages add column if not exists attachment_count integer not null default 0;
alter table messages drop constraint if exists messages_attachment_count_check;
alter table messages add constraint messages_attachment_count_check check (attachment_count >= 0);
alter table messages drop constraint if exists messages_check;
alter table messages drop constraint if exists messages_attachment_content_check;
alter table messages add constraint messages_attachment_content_check
  check (body is not null or media_public_id is not null or attachment_count > 0);

create table if not exists message_attachments (
  id uuid primary key default gen_random_uuid(),
  message_id uuid not null references messages(id) on delete cascade,
  uploader_id uuid not null references profiles(id) on delete cascade,
  storage_path text not null unique,
  original_filename text not null,
  mime_type text not null,
  size_bytes bigint not null check (size_bytes > 0),
  kind text not null check (kind in ('image','video','document','audio')),
  width integer,
  height integer,
  duration numeric,
  created_at timestamptz not null default now()
);
create index if not exists message_attachments_message_idx
  on message_attachments(message_id, created_at);

alter table message_attachments enable row level security;

-- A security-definer helper is needed to check both block directions; ordinary users
-- can only read their own block rows under the existing blocks_own policy.
create or replace function cirq_can_read_message_attachments(p_message_id uuid)
returns boolean
language sql
security definer
stable
set search_path = ''
as $$
  select exists (
    select 1
    from public.messages m
    join public.conversation_members viewer
      on viewer.conversation_id = m.conversation_id and viewer.user_id = auth.uid()
    where m.id = p_message_id
      and m.moderation_status = 'ok'
      and not exists (
        select 1
        from public.conversation_members other_member
        join public.blocks b
          on (b.blocker_id = auth.uid() and b.blocked_id = other_member.user_id)
          or (b.blocked_id = auth.uid() and b.blocker_id = other_member.user_id)
        where other_member.conversation_id = m.conversation_id
          and other_member.user_id <> auth.uid()
      )
  );
$$;
revoke all on function cirq_can_read_message_attachments(uuid) from public;
grant execute on function cirq_can_read_message_attachments(uuid) to authenticated;

drop policy if exists message_attachments_read on message_attachments;
create policy message_attachments_read on message_attachments
  for select to authenticated
  using (cirq_can_read_message_attachments(message_id));
drop policy if exists message_attachments_insert on message_attachments;
create policy message_attachments_insert on message_attachments
  for insert to authenticated
  with check (
    uploader_id = auth.uid()
    and exists (
      select 1 from public.messages m
      where m.id = message_id and m.sender_id = auth.uid() and m.attachment_count > 0
    )
    and cirq_can_read_message_attachments(message_id)
  );

-- Private bucket: browser clients receive no storage.objects policies. Flask uses the
-- server-only service role, authorizes each request, and issues short-lived signed URLs.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'chat-attachments',
  'chat-attachments',
  false,
  52428800,
  array[
    'image/jpeg','image/pjpeg','image/png','image/gif','image/webp','image/heic','image/heif',
    'video/mp4','video/quicktime','video/webm','video/x-matroska','application/x-matroska',
    'video/x-msvideo','video/avi','video/3gpp','video/3gpp2',
    'application/pdf','application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-powerpoint','application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'text/plain','text/csv','application/csv','application/vnd.oasis.opendocument.text',
    'application/rtf','text/rtf','application/zip','application/x-zip-compressed',
    'audio/mpeg','audio/mp3','audio/mp4','audio/x-m4a','audio/wav','audio/x-wav','audio/wave','audio/ogg','application/ogg'
  ]
)
on conflict (id) do update set
  name = excluded.name,
  public = false,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

notify pgrst, 'reload schema';
