-- Rank streams by observed gifting in the Utah window over the last 14 days.
select tiktok_username,
       count(distinct local_date) as observed_days,
       sum(window_gift_units) as window_gift_units,
       sum(total_gift_units) as all_observed_gift_units
from public.tiktok_gifter_summaries
where local_date >= (now() at time zone 'America/Denver')::date - 13
group by tiktok_username
order by window_gift_units desc;

-- Find repeat top gifters across streams/dates. These are lower bounds:
-- people outside the retained top N in an observation are absent.
select g->>'user_id' as user_id,
       array_agg(distinct nullif(g->>'username', '')) as observed_usernames,
       count(distinct s.tiktok_username) as streams_gifted,
       count(distinct s.local_date) as days_in_top_n,
       sum((g->>'gift_units')::bigint) as retained_gift_units,
       sum((g->>'window_gift_units')::bigint) as retained_window_gift_units
from public.tiktok_gifter_summaries s
cross join lateral jsonb_array_elements(s.top_gifters) g
where s.local_date >= (now() at time zone 'America/Denver')::date - 13
group by g->>'user_id'
order by retained_window_gift_units desc;

-- Optional retention cleanup, run manually when desired:
-- delete from public.tiktok_gifter_summaries
-- where updated_at < now() - interval '90 days';
