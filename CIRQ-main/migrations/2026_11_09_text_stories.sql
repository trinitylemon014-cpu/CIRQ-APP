-- Text stories. Safe to run more than once. Run in the Supabase SQL Editor.
-- A story can now be written text on a colour background instead of a photo or video. A text story has no file, so
-- url / public_id may be empty for it; the content check below guarantees every story has either a file or text.
alter table stories add column if not exists text_body text;
alter table stories add column if not exists bg_style text;
alter table stories alter column url drop not null;
alter table stories alter column public_id drop not null;
alter table stories drop constraint if exists stories_media_type_check;
alter table stories add constraint stories_media_type_check check (media_type in ('image','video','text'));
alter table stories drop constraint if exists stories_text_body_len;
alter table stories add constraint stories_text_body_len check (text_body is null or char_length(text_body) between 1 and 280);
alter table stories drop constraint if exists stories_bg_style_check;
alter table stories add constraint stories_bg_style_check check (bg_style is null or bg_style in ('violet','ocean','sunset','forest','rose','midnight','gold','mono'));
alter table stories drop constraint if exists stories_content_check;
alter table stories add constraint stories_content_check check (
  (media_type = 'text' and text_body is not null) or (media_type in ('image','video') and url is not null and public_id is not null));

notify pgrst, 'reload schema';
