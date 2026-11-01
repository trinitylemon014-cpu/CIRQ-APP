-- Add the partial index used by the newest-first, cursor-paginated public reels archive.
-- Safe to run more than once.
create index if not exists posts_public_reels_library_idx on posts(created_at desc, id desc)
  where kind = 'reel' and visibility = 'public' and moderation_status = 'ok' and community_id is null;
