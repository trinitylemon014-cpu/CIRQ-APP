-- Self-service account recovery code. Safe to run more than once.
-- Run in the Supabase SQL editor before deploying the matching code.

-- ---------- ACCOUNT RECOVERY CODE ----------
-- Every student gets one simple code (e.g. K7M2-9QXD-4TFH) at sign-up. If they forget their password they can reset it with
-- their username/email + this code. The server keeps it two ways: an HMAC (to check a submitted code) and an AES-encrypted copy
-- (so the student can look their own code up again after re-entering their password). The plaintext is never stored.
-- This table has RLS on and NO policies: nobody but the server's service role can read or write it.
alter table profiles add column if not exists recovery_saved_at timestamptz;     -- when the student confirmed they saved it (drives the reminder banner)

create table if not exists recovery_codes (
  user_id uuid primary key references profiles(id) on delete cascade,
  code_hash text not null unique,        -- HMAC-SHA256 of the normalised code
  code_enc text not null,                -- Fernet (AES-128-CBC + HMAC) ciphertext of the code
  created_at timestamptz not null default now(),
  last_used_at timestamptz,
  failed_attempts integer not null default 0,
  locked_until timestamptz               -- too many wrong tries: recovery is paused until then
);
alter table recovery_codes enable row level security;
revoke all on recovery_codes from anon, authenticated;
