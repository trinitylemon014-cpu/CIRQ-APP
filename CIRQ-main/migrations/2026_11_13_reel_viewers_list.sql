-- "Who viewed my reel": the owner's viewer list pages newest-first by (reel_id, created_at).
-- Run in Supabase SQL Editor. No new table or policy is needed: reel_views already exists, and the
-- list is read server-side with the service role (clients still have no SELECT policy on viewer rows).
create index if not exists reel_views_reel_created_idx
  on public.reel_views (reel_id, created_at desc);

notify pgrst, 'reload schema';
