-- Paste this entire file into Supabase > SQL Editor > New query > Run.
create table if not exists public.tiktok_gifter_summaries (
    observation_id uuid primary key,
    tiktok_username text not null,
    room_id text not null,
    local_date date not null,
    observation_started_at timestamptz not null,
    updated_at timestamptz not null,
    last_gift_at timestamptz,
    total_gift_units bigint not null default 0 check (total_gift_units >= 0),
    window_gift_units bigint not null default 0 check (window_gift_units >= 0),
    unique_gifters integer not null default 0,
    unknown_price_events integer not null default 0,
    top_gifters jsonb not null default '[]'::jsonb
        check (jsonb_typeof(top_gifters) = 'array' and jsonb_array_length(top_gifters) <= 100)
);
create index if not exists tiktok_gifter_summaries_creator_date
    on public.tiktok_gifter_summaries (tiktok_username, local_date desc);
alter table public.tiktok_gifter_summaries enable row level security;
revoke all on public.tiktok_gifter_summaries from anon, authenticated;
grant select, insert, update on public.tiktok_gifter_summaries to service_role;
comment on table public.tiktok_gifter_summaries is
    'Top-N public gifters per listener observation, overwritten every flush. Gift units are catalog diamond_count times quantity, not USD spent. Window is 18:30-21:30 America/Denver. Partial observation, not official leaderboard.';
