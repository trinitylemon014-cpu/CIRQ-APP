-- Username/email login lookup and active-status preferences.
-- Preflight before running: these queries must return no rows.
-- SELECT lower(username), count(*) FROM public.profiles GROUP BY lower(username) HAVING count(*) > 1;
-- SELECT id, username FROM public.profiles WHERE username LIKE '%@%';
-- SELECT lower(email), count(*) FROM auth.users WHERE email IS NOT NULL GROUP BY lower(email) HAVING count(*) > 1;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM public.profiles
    WHERE username LIKE '%@%'
  ) THEN
    RAISE EXCEPTION 'Cannot add username login constraint: profiles contain usernames with @; review the preflight query.';
  END IF;
  IF EXISTS (
    SELECT 1 FROM public.profiles
    GROUP BY lower(username)
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION 'Cannot add case-insensitive username index: duplicate usernames exist; review the preflight query.';
  END IF;
  IF EXISTS (
    SELECT 1 FROM auth.users
    WHERE email IS NOT NULL
    GROUP BY lower(email)
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION 'Cannot add case-insensitive email index: duplicate emails exist; review the preflight query.';
  END IF;
END
$$;

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS last_seen_at timestamptz,
  ADD COLUMN IF NOT EXISTS show_active_status boolean NOT NULL DEFAULT true;

CREATE UNIQUE INDEX IF NOT EXISTS profiles_username_lower_uidx
  ON public.profiles (lower(username));
CREATE INDEX IF NOT EXISTS profiles_active_status_idx
  ON public.profiles (last_seen_at DESC)
  WHERE show_active_status;

-- Supabase Auth stores emails in auth.users. Do not copy private email addresses to profiles.
CREATE OR REPLACE FUNCTION public.cirq_profile_id_for_email(p_email text)
RETURNS TABLE(user_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT u.id
  FROM auth.users u
  JOIN public.profiles p ON p.id = u.id
  WHERE lower(u.email) = lower(trim(p_email))
  LIMIT 1
$$;
REVOKE ALL ON FUNCTION public.cirq_profile_id_for_email(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cirq_profile_id_for_email(text) TO service_role;

CREATE OR REPLACE FUNCTION public.cirq_ping_presence(p_user_id uuid)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  changed integer;
BEGIN
  UPDATE public.profiles
  SET last_seen_at = now()
  WHERE id = p_user_id
    AND show_active_status
    AND (last_seen_at IS NULL OR last_seen_at <= now() - interval '30 seconds');
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END
$$;
REVOKE ALL ON FUNCTION public.cirq_ping_presence(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cirq_ping_presence(uuid) TO service_role;

-- Rollback (only if you are reverting this feature):
-- DROP FUNCTION IF EXISTS public.cirq_ping_presence(uuid);
-- DROP FUNCTION IF EXISTS public.cirq_profile_id_for_email(text);
-- DROP INDEX IF EXISTS public.profiles_active_status_idx;
-- DROP INDEX IF EXISTS public.profiles_username_lower_uidx;
-- ALTER TABLE public.profiles DROP COLUMN IF EXISTS show_active_status;
-- last_seen_at pre-existed in this project's schema and is intentionally not dropped.
