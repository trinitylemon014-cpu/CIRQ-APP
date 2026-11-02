-- Unlisted-school submissions and auditable, delayed account approval.
-- Run in Supabase SQL Editor after the existing CIRQ schema.
--
-- The app's existing Render cron remains the durable approval scheduler. The
-- pending-page status endpoint also invokes the same conditional app service,
-- so a sleeping/restarted web process cannot strand an eligible account.
--
-- Rollback: stop the Render pending-approvals cron first, then drop the
-- cirq_claim_school_submission_slot function, rate-limit table, school_submissions
-- table, and the added profile columns/indexes. Dropping these columns discards
-- school-submission and approval-source history.

create table if not exists public.school_submissions (
  id uuid primary key default gen_random_uuid(),
  name text not null check (char_length(name) between 3 and 100),
  normalized_name text not null,
  submitted_by uuid references public.profiles(id) on delete set null,
  status text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
  resolved_school_id uuid references public.schools(id) on delete set null,
  reviewed_by uuid references public.profiles(id) on delete set null,
  reviewed_at timestamptz,
  decision_note text,
  created_at timestamptz not null default now()
);

create unique index if not exists school_submissions_one_pending_name_idx
  on public.school_submissions(normalized_name) where status = 'pending';
create index if not exists school_submissions_status_created_idx
  on public.school_submissions(status, created_at);

alter table public.profiles
  add column if not exists submitted_school_name text,
  add column if not exists school_submission_id uuid references public.school_submissions(id) on delete set null,
  add column if not exists school_verified boolean not null default false,
  add column if not exists auto_approval_blocked boolean not null default false,
  add column if not exists approval_source text check (approval_source in ('admin', 'system')),
  add column if not exists approved_by uuid references public.profiles(id) on delete set null,
  add column if not exists approved_at timestamptz;

create index if not exists profiles_school_submission_idx on public.profiles(school_submission_id);
create index if not exists profiles_auto_approved_idx
  on public.profiles(approved_at desc) where approval_source = 'system';

create table if not exists public.school_submission_rate_limits (
  ip_hash text primary key,
  submission_date date not null,
  attempts integer not null check (attempts > 0)
);
alter table public.school_submission_rate_limits enable row level security;

create or replace function public.cirq_claim_school_submission_slot(p_ip_hash text, p_limit integer)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  claimed boolean;
begin
  if p_limit < 1 then
    return false;
  end if;

  insert into public.school_submission_rate_limits as current_limit
    (ip_hash, submission_date, attempts)
  values (p_ip_hash, current_date, 1)
  on conflict (ip_hash) do update
    set submission_date = current_date,
        attempts = case
          when current_limit.submission_date < current_date then 1
          else current_limit.attempts + 1
        end
    where current_limit.submission_date < current_date
       or current_limit.attempts < p_limit
  returning true into claimed;

  return coalesce(claimed, false);
end;
$$;

revoke all on function public.cirq_claim_school_submission_slot(text, integer) from public, anon, authenticated;
grant execute on function public.cirq_claim_school_submission_slot(text, integer) to service_role;

alter table public.school_submissions enable row level security;
drop policy if exists school_submissions_admin_read on public.school_submissions;
create policy school_submissions_admin_read on public.school_submissions
  for select to authenticated
  using (exists (
    select 1 from public.profiles p
    where p.id = auth.uid() and p.role = 'admin' and p.account_status = 'approved'
  ));

create or replace function public.cirq_protect_profile_columns()
returns trigger language plpgsql as $$
begin
  if auth.role() <> 'service_role' then
    new.role := old.role; new.account_status := old.account_status; new.verified := old.verified;
    new.session_epoch := old.session_epoch; new.email_verified_at := old.email_verified_at;
    new.status_note := old.status_note; new.id := old.id;
    new.coin_balance := old.coin_balance; new.boosted_until := old.boosted_until;
    new.school_verified := old.school_verified;
    new.submitted_school_name := old.submitted_school_name;
    new.school_submission_id := old.school_submission_id;
    new.auto_approval_blocked := old.auto_approval_blocked;
    new.approval_source := old.approval_source;
    new.approved_by := old.approved_by;
    new.approved_at := old.approved_at;
  end if;
  return new;
end $$;
