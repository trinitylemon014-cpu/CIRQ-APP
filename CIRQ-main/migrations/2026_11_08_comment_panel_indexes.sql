-- Keyset-pagination indexes for the comment panel and the Saved tab. Safe to run more than once; no data changes.
create index if not exists comments_post_page_idx on comments(post_id, created_at desc, id desc) where moderation_status = 'ok';
create index if not exists comment_likes_user_idx on comment_likes(user_id, comment_id);
create index if not exists likes_user_post_idx on likes(user_id, post_id);
notify pgrst, 'reload schema';
