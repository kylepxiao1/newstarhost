import asyncio
from datetime import datetime, date, timezone
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import top_gifters_bot as bot

TZ = ZoneInfo('America/Denver')
DAY = date(2026, 10, 15)
EVENT = {'id': 'test', 'summary': 'Wildcardz Live',
         'start': {'dateTime': '2026-10-15T18:30:00-06:00'},
         'end': {'dateTime': '2026-10-15T21:30:00-06:00'}}


class TopGiftersTests(unittest.IsolatedAsyncioTestCase):
    async def check_evening_schedule(self, stale_daily_flag=False, fail_first_send=False):
        with tempfile.TemporaryDirectory() as directory:
            cfg = bot.Config('fixture', 'fixture', 'Wildcardz Live', TZ.key, 60,
                Path(directory)/'state.json', 'https://example.test/webhook',
                'https://example.test', 'fixture', 'wildcard_ns', 2, True, False)
            times = [datetime(2026, 10, 15, 19, 15, tzinfo=TZ),
                     datetime(2026, 10, 15, 21, 31, tzinfo=TZ)]
            state = {'sent_events': {}}
            if stale_daily_flag:
                state['last_daily_run_local_date'] = DAY.isoformat()
            sleeps = [0]
            class Clock:
                @classmethod
                def now(cls, tz=None):
                    return times[min(sleeps[0], 1)].astimezone(tz or timezone.utc)

            sender = AsyncMock(side_effect=[RuntimeError('temporary delivery error'), None] if fail_first_send else None)
            async def advance(seconds):
                if sleeps[0] == 0:
                    self.assertNotIn('test|', str(state.get('sent_events')))
                    self.assertEqual(sender.await_count, 0)
                sleeps[0] += 1
                if sleeps[0] >= (3 if fail_first_send else 2):
                    raise asyncio.CancelledError()

            with patch.object(bot, '_load_config', return_value=cfg), \
                 patch.object(bot, '_load_state', return_value=state), \
                 patch.object(bot, '_fetch_google_events', new=AsyncMock(return_value=[EVENT])), \
                 patch.object(bot, '_send_report_for_event', sender), \
                 patch.object(bot, 'datetime', Clock), \
                 patch.object(bot.asyncio, 'sleep', advance):
                # Preserve real datetime.combine, needed by run().
                Clock.combine = datetime.combine
                Clock.fromisoformat = datetime.fromisoformat
                with self.assertRaises(asyncio.CancelledError):
                    await bot.run()
            self.assertEqual(sender.await_count, 2 if fail_first_send else 1)
            self.assertEqual(state['last_daily_run_local_date'], DAY.isoformat())
            self.assertEqual(len(state['sent_events']), 1)
            self.assertTrue(cfg.state_file.exists())

    async def test_waits_until_930_stream_finishes(self):
        await self.check_evening_schedule()

    async def test_recovers_old_empty_daily_check_state(self):
        await self.check_evening_schedule(stale_daily_flag=True)

    async def test_failed_delivery_is_retried(self):
        await self.check_evening_schedule(fail_first_send=True)

    async def test_successful_report_suppresses_duplicate_daily_send(self):
        state = {'last_daily_run_local_date': DAY.isoformat(), 'sent_events': {
            'event': {'event_start_utc': '2026-10-16T00:30:00+00:00'}}}
        self.assertTrue(bot._daily_report_already_sent(state, DAY, TZ))
        self.assertFalse(bot._daily_report_already_sent(state, date(2026, 10, 16), TZ))

    async def test_report_uses_original_gift_table_and_filters_host(self):
        requests, posts = [], []
        def db(request):
            requests.append(request)
            return httpx.Response(200, json=[
                {'to_member_nickname':'Kyle','from_username':'alice','amount_value':100},
                {'to_member_nickname':'Kyle','from_username':'alice','amount_value':50},
                {'to_member_nickname':'Kyle','from_username':'bob','diamond_count':10,'repeat_count':2},
                {'to_member_nickname':'the host 1','from_username':'alice','amount_value':9000}])
        def webhook(request):
            import json
            posts.append(json.loads(request.content)['content'])
            return httpx.Response(204)
        cfg = bot.Config('', '', 'Wildcardz Live', TZ.key, 60, Path('/unused'),
            'https://example.test/webhook', 'https://example.test', '', 'wildcard_ns', 2, True, False)
        async with httpx.AsyncClient(base_url='https://example.test/rest/v1', transport=httpx.MockTransport(db)) as database, \
                   httpx.AsyncClient(transport=httpx.MockTransport(webhook)) as delivery:
            await bot._send_report_for_event(supabase_client=database, webhook_client=delivery,
                cfg=cfg, tz=TZ, event=EVENT,
                start_dt=bot._parse_event_datetime(EVENT['start']),
                end_dt=bot._parse_event_datetime(EVENT['end']), logger=logging.getLogger('test'))
        self.assertEqual(requests[0].url.path, '/rest/v1/gift_events')
        self.assertEqual(requests[0].url.params['tiktok_username'], 'eq.wildcard_ns')
        self.assertEqual(len(requests[0].url.params.get_list('iso_ts')), 2)
        self.assertEqual(len(posts), 1)
        self.assertIn('Kyle', posts[0])
        self.assertIn('150', posts[0])
        self.assertNotIn('the host', posts[0])
        self.assertLessEqual(len(posts[0]), 1900)


if __name__ == '__main__':
    unittest.main()
