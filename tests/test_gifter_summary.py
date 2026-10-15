import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import argparse
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tiktok_gifter_summary import Summary, Store, in_window, listen, main
from TikTokLive.events import ConnectEvent, GiftEvent

NOW = datetime(2026, 10, 16, 1, 0, tzinfo=timezone.utc)


def gift(uid='1', count=1, price=10, mid=100, group=55, streakable=True, streaking=True):
    return NS(user=NS(id=uid, unique_id='user'+uid, nickname='User '+uid),
              gift=NS(id=5, diamond_count=price, streakable=streakable),
              common=NS(msg_id=mid), group_id=group, repeat_count=count, streaking=streaking)


class AggregationTests(unittest.TestCase):
    def test_combo_progress_duplicate_final_and_regression(self):
        summary = Summary('host', '123', NOW)
        for count in [1, 3, 2, 3, 5, 5]:
            summary.add(gift(count=count), NOW)
        summary.add(gift(count=5, mid=101, streaking=False), NOW)
        self.assertEqual(summary.snapshot(20, NOW)['total_gift_units'], 50)
        summary.add(gift(count=2, mid=102, group=56), NOW)
        self.assertEqual(summary.snapshot(20, NOW)['total_gift_units'], 70)

    def test_missing_group_waits_for_final(self):
        summary = Summary('host', '123', NOW)
        summary.add(gift(group=0, count=3), NOW)
        self.assertFalse(summary.gifters)
        event = gift(group=0, count=3, streaking=False)
        summary.add(event, NOW)
        summary.add(event, NOW)
        self.assertEqual(summary.snapshot(20, NOW)['total_gift_units'], 30)

    def test_top_n_is_replaced_and_room_totals_include_everyone(self):
        summary = Summary('host', '123', NOW)
        summary.add(gift(uid='1', price=100, mid=1, streakable=False), NOW)
        summary.add(gift(uid='2', price=50, mid=2, streakable=False), NOW)
        before = summary.snapshot(1, NOW)
        summary.add(gift(uid='2', price=200, mid=3, streakable=False), NOW)
        after = summary.snapshot(1, NOW)
        self.assertEqual(after['observation_id'], before['observation_id'])
        self.assertEqual(after['top_gifters'][0]['user_id'], '2')
        self.assertEqual(after['total_gift_units'], 350)
        self.assertEqual(after['unique_gifters'], 2)
        self.assertNotIn('event', after)

    def test_dst_and_half_open_window(self):
        for month, start_utc, end_utc in [(10, 0, 3), (12, 1, 4)]:
            self.assertTrue(in_window(datetime(2026, month, 16, start_utc, 30, tzinfo=timezone.utc)))
            self.assertFalse(in_window(datetime(2026, month, 16, end_utc, 30, tzinfo=timezone.utc)))
        self.assertFalse(in_window(datetime(2026, 10, 16, 0, 29, tzinfo=timezone.utc)))

    def test_unknown_price_not_invented(self):
        summary = Summary('host', '123', NOW)
        summary.add(gift(price=0), NOW)
        self.assertEqual(summary.unknown_price_events, 1)
        self.assertEqual(summary.snapshot(20, NOW)['total_gift_units'], 0)

    def test_nonstreak_duplicate_ignores_final_marker(self):
        summary = Summary('host', '123', NOW)
        summary.add(gift(streakable=False, streaking=True), NOW)
        summary.add(gift(streakable=False, streaking=False), NOW)
        self.assertEqual(summary.snapshot(20, NOW)['total_gift_units'], 10)

    def test_real_tiktok_event_shape(self):
        from TikTokLive.events import GiftEvent
        event = GiftEvent().from_dict({'baseMessage': {'messageId': '123'},
            'fromUser': {'id': '9', 'username': 'real_shape', 'nickName': 'Test'},
            'mGift': {'id': '5', 'diamondCount': 10, 'type': 1},
            'groupId': '44', 'repeatCount': 3, 'repeatEnd': 1})
        summary = Summary('host', '123', NOW)
        summary.add(event, NOW)
        self.assertEqual(summary.snapshot(20, NOW)['total_gift_units'], 30)


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_snapshot_retried_and_only_summary_table_written(self):
        calls = []
        failed = [False]
        def handler(request):
            calls.append(request)
            if not failed[0]:
                failed[0] = True
                return httpx.Response(503)
            return httpx.Response(201)
        store = Store.__new__(Store)
        store.pending, store.lock = {}, asyncio.Lock()
        store.client = httpx.AsyncClient(base_url='https://example.test/rest/v1/', transport=httpx.MockTransport(handler))
        first, second = Summary('host', '123', NOW), Summary('host', '456', NOW)
        try:
            with self.assertRaises(httpx.HTTPStatusError):
                await store.save(first, 20)
            await store.save(second, 20)
            self.assertFalse(store.pending)
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(r.url.path == '/rest/v1/tiktok_gifter_summaries' for r in calls))
        finally:
            await store.client.aclose()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_retains_totals_and_shutdown_flush_survives_disconnect_error(self):
        stop = asyncio.Event()
        snapshots = []
        subscribed = []
        clients = []

        class FakeClient:
            def __init__(self, **kwargs):
                self.room_id = 123
                self.handlers = {}
                self.connections = 0
                clients.append(self)

            def on(self, event_type):
                subscribed.append(event_type)
                def register(callback):
                    self.handlers[event_type] = callback
                    return callback
                return register

            async def connect(self, **kwargs):
                self.connections += 1
                await self.handlers[ConnectEvent](NS())
                await self.handlers[GiftEvent](gift(mid=self.connections, streakable=False))
                if self.connections == 2:
                    stop.set()

            async def disconnect(self, close_client=False):
                if close_client:
                    raise RuntimeError('simulated socket cleanup failure')

        class FakeStore:
            pending = {}
            async def save(self, summary, top_n):
                snapshots.append(summary.snapshot(top_n, NOW))

        waits = [0]
        original_wait_for = asyncio.wait_for
        async def quick_wait(awaitable, timeout):
            # Simulate retry delay without slowing the test.
            if timeout >= 60 and not stop.is_set():
                waits[0] += 1
                awaitable.close()
                raise asyncio.TimeoutError()
            return await original_wait_for(awaitable, timeout=1)

        args = argparse.Namespace(top=20, flush_seconds=60, all_day=True)
        with patch('tiktok_gifter_summary.TikTokLiveClient', FakeClient), \
             patch('tiktok_gifter_summary.asyncio.wait_for', quick_wait):
            await listen('host', FakeStore(), args, stop)
        self.assertEqual(set(subscribed), {ConnectEvent, GiftEvent})
        self.assertEqual(clients[0].connections, 2)
        self.assertTrue(clients[0].ignore_broken_payload)
        self.assertEqual(len({s['observation_id'] for s in snapshots}), 1)
        self.assertEqual(snapshots[-1]['total_gift_units'], 20)
        self.assertGreaterEqual(len(snapshots), 3)

    async def test_cancel_main_drains_listeners_before_closing_store(self):
        from unittest.mock import AsyncMock
        running = asyncio.Event()
        drained = asyncio.Event()
        closed = [False]

        async def close_store():
            self.assertTrue(drained.is_set())
            closed[0] = True

        fake_store = NS(check=AsyncMock(), client=NS(aclose=close_store))
        async def active_listener(username, store, args, stop):
            running.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.assertFalse(closed[0])
                drained.set()

        args = argparse.Namespace(usernames='host', check=False)
        with patch('tiktok_gifter_summary.Store', return_value=fake_store), \
             patch('tiktok_gifter_summary.listen', active_listener), \
             patch.object(asyncio.get_running_loop(), 'add_signal_handler'):
            task = asyncio.create_task(main(args))
            await asyncio.wait_for(running.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(closed[0])

    async def test_outside_window_makes_no_connection(self):
        stop = asyncio.Event()
        client = NS(ignore_broken_payload=False)
        from unittest.mock import AsyncMock
        client.disconnect = AsyncMock()
        client.connect = AsyncMock()
        client.on = lambda event_type: lambda callback: callback
        store = NS(save=AsyncMock(), pending={})
        args = argparse.Namespace(top=20, flush_seconds=60, all_day=False)
        async def end_wait(awaitable, timeout):
            awaitable.close()
            stop.set()
            return True

        with patch('tiktok_gifter_summary.TikTokLiveClient', return_value=client), \
             patch('tiktok_gifter_summary.in_window', return_value=False), \
             patch('tiktok_gifter_summary.asyncio.wait_for', end_wait):
            await listen('host', store, args, stop)
        client.connect.assert_not_awaited()
        store.save.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
