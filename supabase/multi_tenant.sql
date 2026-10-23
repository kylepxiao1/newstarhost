-- Multi-group (multi-tenant) storage for NewStarHost. Replaces media/*.json.
-- Run once in the Supabase SQL editor. Safe to re-run.

create extension if not exists pgcrypto;

-- One row per dance group = one login.
create table if not exists public.groups (
  id uuid primary key default gen_random_uuid(),
  username text not null unique,             -- login name (lowercase)
  display_name text not null,                -- shown in the app, used as the group name
  tiktok_handle text,                        -- the group's TikTok account (no @)
  password_hash text not null,               -- PBKDF2-SHA256; set by scripts/manage_groups.py
  session_version integer not null default 1,-- bump to log out every device
  overlay_key text not null unique,          -- secret key for read-only OBS overlay links
  disabled boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Song library: one row per song (data = the song's fields: name, url, dancers, roles...).
create table if not exists public.group_songs (
  group_id uuid not null references public.groups(id) on delete cascade,
  song_id text not null,
  data jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (group_id, song_id)
);

-- Small per-group documents: dancers, settings (hotkeys), role_presets, rpd_queries,
-- gift_notes, play_counts/points_counts (offline fallback).
create table if not exists public.group_kv (
  group_id uuid not null references public.groups(id) on delete cascade,
  key text not null,
  value jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (group_id, key)
);

-- Tie existing history tables to a group.
alter table public.play_events add column if not exists group_id uuid;
alter table public.stream_notes add column if not exists group_id uuid;
alter table public.battle_results add column if not exists group_id uuid;
alter table public.access_logs add column if not exists group_id uuid;
create index if not exists play_events_group_idx on public.play_events (group_id);
create index if not exists stream_notes_group_idx on public.stream_notes (group_id, stream_date);
create index if not exists battle_results_group_idx on public.battle_results (group_id);
create index if not exists access_logs_group_idx on public.access_logs (group_id, ts desc);

-- Only the server (secret key) may touch these. The publishable key is sent to every
-- browser, and groups holds password hashes.
alter table public.groups enable row level security;
alter table public.group_songs enable row level security;
alter table public.group_kv enable row level security;

-- Public audio bucket, served through Supabase's CDN. Objects are content-addressed
-- (<group_id>/<sha256>.mp3) and uploaded with a 1-year immutable cache header.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('song-audio', 'song-audio', true, 52428800, array['audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/x-wav', 'audio/mp4', 'audio/aac', 'audio/ogg'])
on conflict (id) do update set public = true;

-- TikTok identity by numeric id: handles can change, ids don't. past_handles keeps history
-- recorded under a group's old handle visible after a handle change.
alter table public.groups add column if not exists tiktok_id text;
alter table public.groups add column if not exists past_handles text[] not null default '{}';

-- Song recency for suggestions, per group ("plays since this song was last played").
-- Replaces the browser reading play_counts_after_last, which mixed every group's plays.
create or replace function public.plays_since_last(gid uuid, exclude text[] default '{}')
returns table(song_name text, plays_after_last bigint)
language sql stable security definer set search_path = public as $$
  with ev as (
    select p.song_name, p.unix_ts from public.play_events p
    where p.group_id = gid and not (p.song_name = any(exclude))
  ), last_play as (
    select ev.song_name, max(ev.unix_ts) as last_ts from ev group by ev.song_name
  )
  select lp.song_name, (select count(*) from ev where ev.unix_ts > lp.last_ts) as plays_after_last
  from last_play lp;
$$;
revoke all on function public.plays_since_last(uuid, text[]) from public, anon, authenticated;

-- Groups must not see each other: no public reads of play history or stream notes
-- (the app reads them server-side, filtered to the logged-in group).
drop policy if exists "anon read play_events" on public.play_events;
drop policy if exists "Allow read access to stream_notes" on public.stream_notes;
revoke select on public.play_counts_after_last from anon, authenticated;
