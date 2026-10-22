-- Access log for the NewStarHost app (written by backend/access_log.py).
-- Run once in the Supabase SQL editor.

create table if not exists public.access_logs (
  id bigint generated always as identity primary key,
  ts timestamptz not null default now(),
  kind text not null,              -- 'request' (server), 'client' (browser beacon), 'websocket'
  visitor_id text,                 -- anonymous per-browser cookie (nsh_vid)
  session_id text,                 -- per-tab ID from the browser beacon
  ip text,
  forwarded_for text,
  fly_region text,                 -- Fly edge region that served the request (rough location)
  host text,
  method text,
  path text,
  query text,
  status integer,
  duration_ms integer,
  user_agent text,
  referer text,
  accept_language text,
  client jsonb                     -- browser details: screen, timezone, languages, device, connection...
);

create index if not exists access_logs_ts_idx on public.access_logs (ts desc);
create index if not exists access_logs_visitor_idx on public.access_logs (visitor_id, ts desc);
create index if not exists access_logs_ip_idx on public.access_logs (ip, ts desc);

-- The publishable key is sent to every browser (GET /config), so lock the table down:
-- with RLS on and no policies, only the server's secret key can read or write it.
alter table public.access_logs enable row level security;
