-- User-authored sponsored ads, shown only while the account boost is active. Safe to run more than once.
create table if not exists boost_ads (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique references profiles(id) on delete cascade,
  headline text not null check (char_length(headline) between 2 and 80),
  body text not null check (char_length(body) between 5 and 300),
  image_url text not null,
  image_public_id text not null,
  updated_at timestamptz not null default now()
);
create index if not exists boost_ads_updated_idx on boost_ads(updated_at desc);
alter table boost_ads enable row level security;

notify pgrst, 'reload schema';
