import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import logging
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import reminder_bot as bot

TZ = ZoneInfo('America/Denver')


def config(path, test_fire=False):
    return bot.Config(calendar_id='fixture', calendar_api_key='fixture',
        webhook_url='https://example.test/webhook', event_name='Wildcardz Live',
        practice_event_name='Wildcardz Practice', supabase_url='https://example.test',
        supabase_key='fixture', timezone_name=TZ.key, run_hour=12, run_minute=0,
        poll_seconds=30, state_file=path, send_no_event_message=False,
        test_fire_now=test_fire)


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def run_clock(self, start, times, test_fire=False):
        with tempfile.TemporaryDirectory() as directory:
            cfg = config(Path(directory)/'state.json', test_fire)
            tick = [0]
            class Clock:
                combine = datetime.combine
                @classmethod
                def now(cls, tz=None):
                    return times[min(tick[0], len(times)-1)].astimezone(tz or timezone.utc)
            async def advance(seconds):
                tick[0] += 1
                if tick[0] >= len(times):
                    raise asyncio.CancelledError()
            sender = AsyncMock(return_value=True)
            with patch.object(bot, '_load_config', return_value=cfg), \
                 patch.object(bot, '_load_state', return_value=start), \
                 patch.object(bot, '_run_notes_followups', new=AsyncMock(return_value=False)), \
                 patch.object(bot, '_run_for_day', sender), \
                 patch.object(bot, 'datetime', Clock), \
                 patch.object(bot.asyncio, 'sleep', advance):
                with self.assertRaises(asyncio.CancelledError):
                    await bot.run()
            return sender.await_count, start

    async def test_afternoon_redeploy_with_empty_state_does_not_send(self):
        count, state = await self.run_clock({}, [datetime(2026, 10, 15, 17, tzinfo=TZ)])
        self.assertEqual(count, 0)
        self.assertEqual(state['startup_skip_day'], '2026-10-15')

    async def test_afternoon_redeploy_blocks_stale_test_fire_flag(self):
        count, _ = await self.run_clock({}, [datetime(2026, 10, 15, 17, tzinfo=TZ)], True)
        self.assertEqual(count, 0)

    async def test_normal_noon_reminder_still_sends_once(self):
        times = [datetime(2026, 10, 15, 11, 59, tzinfo=TZ),
                 datetime(2026, 10, 15, 12, 1, tzinfo=TZ),
                 datetime(2026, 10, 15, 12, 2, tzinfo=TZ)]
        count, state = await self.run_clock({}, times)
        self.assertEqual(count, 1)
        self.assertEqual(state['last_sent_day'], '2026-10-15')

    async def test_skip_today_does_not_disable_tomorrow(self):
        times = [datetime(2026, 10, 15, 17, tzinfo=TZ),
                 datetime(2026, 10, 16, 12, 1, tzinfo=TZ)]
        count, state = await self.run_clock({}, times)
        self.assertEqual(count, 1)
        self.assertEqual(state['last_sent_day'], '2026-10-16')

    async def test_existing_sent_state_does_not_resend(self):
        count, _ = await self.run_clock({'last_sent_day':'2026-10-15'},
            [datetime(2026, 10, 15, 17, tzinfo=TZ)])
        self.assertEqual(count, 0)

    async def test_elapsed_notes_followup_not_replayed_but_new_due_time_runs(self):
        now = datetime.now(timezone.utc)
        event = {'id':'fixture','summary':'Wildcardz Live',
            'start':{'dateTime':(now-timedelta(hours=5)).isoformat()},
            'end':{'dateTime':(now-timedelta(hours=2)).isoformat()}}
        cfg = config(Path('/unused'))
        sender = AsyncMock()
        note_check = AsyncMock(return_value=False)
        with patch.object(bot, '_fetch_google_events', new=AsyncMock(return_value=[event])), \
             patch.object(bot, '_stream_note_exists_for_day', note_check), \
             patch.object(bot, '_send_webhook_message', sender):
            await bot._run_notes_followups(client=None,cfg=cfg,tz=TZ,logger=logging.getLogger('test'),
                state={},startup_utc=now)
            sender.assert_not_awaited()
            note_check.assert_not_awaited()
            await bot._run_notes_followups(client=None,cfg=cfg,tz=TZ,logger=logging.getLogger('test'),
                state={},startup_utc=now-timedelta(hours=3))
            sender.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
