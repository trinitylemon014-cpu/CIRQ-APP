-- Editable game-only developer card details. Safe to run more than once.
create table if not exists developer_card_settings (
  id text primary key default 'developer' check (id = 'developer'),
  display_name text,
  image_url text,
  image_public_id text,
  updated_by uuid references profiles(id) on delete set null,
  updated_at timestamptz not null default now()
);
insert into developer_card_settings(id) values ('developer') on conflict (id) do nothing;
alter table developer_card_settings enable row level security;

notify pgrst, 'reload schema';
