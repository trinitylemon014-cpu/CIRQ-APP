-- Coin gifts and loans between friends. Safe to run more than once.
-- Run in the Supabase SQL editor (or `psql`) before deploying the matching code.

-- ---------- COIN GIFTS & LOANS ----------
-- Gifts are instant and final. Loans are a two-party agreement: the lender offers, the borrower accepts ("I agree"),
-- coins move only on acceptance, and on the due date the app collects automatically (see app/services/lending.py).
-- Every movement of coins goes through cirq_transfer_coins (atomic, idempotent on ref); the loan's own history is
-- the append-only coin_loan_events table, whose (loan_id, ref) key makes a retried collection impossible to double-count.

-- Atomic two-sided transfer. Returns the sender's new balance, -1 if this ref was already processed, -2 if the sender
-- has too few coins, -3 for an invalid request. Both rows are locked in a fixed order so two transfers can't deadlock.
create or replace function cirq_transfer_coins(p_from uuid, p_to uuid, p_amount integer, p_reason_out text, p_reason_in text, p_ref text)
returns integer language plpgsql security definer as $$
declare inserted integer; sender_balance integer;
begin
  if p_amount is null or p_amount <= 0 or p_from = p_to then return -3; end if;
  if p_from < p_to then
    perform 1 from profiles where id = p_from for update; perform 1 from profiles where id = p_to for update;
  else
    perform 1 from profiles where id = p_to for update; perform 1 from profiles where id = p_from for update;
  end if;
  if (select coin_balance from profiles where id = p_from) < p_amount then return -2; end if;
  insert into coin_ledger(user_id, amount, reason, ref) values (p_from, -p_amount, p_reason_out, coalesce(p_ref, ''))
    on conflict (user_id, reason, ref) do nothing;
  get diagnostics inserted = row_count;
  if inserted = 0 then return -1; end if;
  insert into coin_ledger(user_id, amount, reason, ref) values (p_to, p_amount, p_reason_in, coalesce(p_ref, ''))
    on conflict (user_id, reason, ref) do nothing;
  update profiles set coin_balance = coin_balance - p_amount where id = p_from returning coin_balance into sender_balance;
  update profiles set coin_balance = coin_balance + p_amount where id = p_to;
  return sender_balance;
end $$;
revoke all on function cirq_transfer_coins(uuid, uuid, integer, text, text, text) from public, anon, authenticated;

create table if not exists coin_gifts (
  id uuid primary key default gen_random_uuid(),
  sender_id uuid not null references profiles(id) on delete cascade,
  recipient_id uuid not null references profiles(id) on delete cascade,
  amount integer not null check (amount between 1 and 100000),
  message text check (char_length(message) <= 200),
  created_at timestamptz not null default now(),
  check (sender_id <> recipient_id)
);
create index if not exists coin_gifts_sender_idx on coin_gifts(sender_id, created_at desc);
create index if not exists coin_gifts_recipient_idx on coin_gifts(recipient_id, created_at desc);

create table if not exists coin_loans (
  id uuid primary key default gen_random_uuid(),
  lender_id uuid not null references profiles(id) on delete cascade,
  borrower_id uuid not null references profiles(id) on delete cascade,
  amount integer not null check (amount between 1 and 100000),
  fee_pct integer not null default 20 check (fee_pct between 0 and 20),   -- late fee, charged once on the unpaid part at the due date
  due_at timestamptz not null,                                            -- end of the chosen day, Kampala time
  message text check (char_length(message) <= 200),
  status text not null default 'offered' check (status in ('offered','active','overdue','repaid','declined','cancelled','expired','forgiven')),
  expires_at timestamptz not null,                                        -- an unanswered offer lapses after 48 hours
  accepted_at timestamptz,
  fee_amount integer not null default 0 check (fee_amount >= 0),          -- fixed once, at the due date
  repaid_total integer not null default 0 check (repaid_total >= 0),      -- principal first, then the fee
  forgiven_amount integer not null default 0 check (forgiven_amount >= 0),
  overdue_at timestamptz,
  closed_at timestamptz,
  frozen boolean not null default false,                                  -- admin safety switch: no collection while true
  terms jsonb not null,                                                   -- the agreement exactly as both people saw it
  terms_hash text not null,                                               -- sha256 of that agreement
  created_at timestamptz not null default now(),
  check (lender_id <> borrower_id)
);
create index if not exists coin_loans_lender_idx on coin_loans(lender_id, status);
create index if not exists coin_loans_borrower_idx on coin_loans(borrower_id, status);
create index if not exists coin_loans_status_due_idx on coin_loans(status, due_at);

create table if not exists coin_loan_events (
  id uuid primary key default gen_random_uuid(),
  loan_id uuid not null references coin_loans(id) on delete cascade,
  kind text not null,            -- offered | accepted | declined | cancelled | expired | reminder_3d | reminder_1d | reminder_due | payment | overdue | fee_applied | repaid | forgiven | frozen | unfrozen
  amount integer not null default 0,
  actor_id uuid references profiles(id) on delete set null,
  note text,
  ref text not null,             -- idempotency key
  created_at timestamptz not null default now(),
  unique (loan_id, ref)
);
create index if not exists coin_loan_events_loan_idx on coin_loan_events(loan_id, created_at);

alter table coin_gifts enable row level security;
alter table coin_loans enable row level security;
alter table coin_loan_events enable row level security;
drop policy if exists gifts_own on coin_gifts; create policy gifts_own on coin_gifts for select to authenticated using (sender_id = auth.uid() or recipient_id = auth.uid());
drop policy if exists loans_parties on coin_loans; create policy loans_parties on coin_loans for select to authenticated using (lender_id = auth.uid() or borrower_id = auth.uid());
drop policy if exists loan_events_parties on coin_loan_events; create policy loan_events_parties on coin_loan_events for select to authenticated
  using (exists (select 1 from coin_loans l where l.id = loan_id and (l.lender_id = auth.uid() or l.borrower_id = auth.uid())));
