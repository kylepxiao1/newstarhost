"""Separate gift-only listener; one compact top-N snapshot per observation.

No event store, backend calls, raw event logging, comments, joins, or likes.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict
from datetime import datetime, time, timezone
import logging
import random
import signal
import uuid
from zoneinfo import ZoneInfo

import httpx
from TikTokLive import TikTokLiveClient
from TikTokLive.events import ConnectEvent, GiftEvent
from TikTokLive.client.web.web_settings import WebDefaults
from utils import _env

LOG = logging.getLogger('gifter-summary')
TABLE = 'tiktok_gifter_summaries'
TZ = ZoneInfo('America/Denver')


def in_window(now):
    local = now.astimezone(TZ).time().replace(tzinfo=None)
    return time(18, 30) <= local < time(21, 30)


class Summary:
    def __init__(self, username, room_id, now):
        self.id = str(uuid.uuid4())
        self.username, self.room_id = username, str(room_id)
        self.started = now
        self.last = now
        self.gifters = {}
        self.seen = OrderedDict()
        self.combos = OrderedDict()
        self.unknown_price_events = 0

    def add(self, event, now):
        user = getattr(event, 'user', None) or getattr(event, 'from_user', None)
        if user is None:
            return
        handle = getattr(user, 'unique_id', '') or getattr(user, 'username', '')
        uid = str(getattr(user, 'id', '') or handle or '')
        if not uid:
            return
        gift = getattr(event, 'gift', None) or getattr(event, 'm_gift', None)
        if gift is None:
            return
        streakable = getattr(gift, 'streakable', getattr(gift, 'type', 0) == 1)
        group = str(getattr(event, 'group_id', 0) or '')
        base = getattr(event, 'common', None) or getattr(event, 'base_message', None)
        mid = str(getattr(base, 'msg_id', 0) or getattr(base, 'message_id', 0) or '')
        count = max(1, int(event.repeat_count or 1))
        final = bool(getattr(event, 'repeat_end', False))
        if hasattr(event, 'gift'):
            final = final or not getattr(event, 'streaking', True)
        # Some messages retain an ID throughout a combo; include counter and final.
        key = (mid, uid, str(gift.id), group, count, final if streakable else False)
        if mid and mid != '0':
            if key in self.seen:
                return
            self.seen[key] = True
            if len(self.seen) > 100000:
                self.seen.popitem(last=False)
        if streakable:
            if group and group != '0':
                combo_key = (uid, str(gift.id), group)
                previous = self.combos.get(combo_key, 0)
                self.combos[combo_key] = max(previous, count)
                count = max(0, count - previous)
                if len(self.combos) > 100000:
                    self.combos.popitem(last=False)
            elif not final:
                # Without a stable combo ID, only count the final total.
                return
        if count == 0:
            return
        price = int(gift.diamond_count or 0)
        if price <= 0:
            self.unknown_price_events += 1
            return
        value = price * count
        row = self.gifters.setdefault(uid, {
            'user_id': uid, 'username': '', 'nickname': '', 'gift_units': 0,
            'gift_quantity': 0, 'window_gift_units': 0,
        })
        row['username'] = str(handle or row['username'])
        row['nickname'] = str(getattr(user, 'nickname', '') or getattr(user, 'nick_name', '') or row['nickname'])
        row['gift_units'] += value
        row['gift_quantity'] += count
        if in_window(now):
            row['window_gift_units'] += value
        self.last = now

    def snapshot(self, top_n, now):
        top = sorted(self.gifters.values(), key=lambda x: (-x['gift_units'], x['user_id']))[:top_n]
        return {
            'observation_id': self.id, 'tiktok_username': self.username,
            'room_id': self.room_id, 'local_date': self.started.astimezone(TZ).date().isoformat(),
            'observation_started_at': self.started.isoformat(), 'updated_at': now.isoformat(),
            'last_gift_at': self.last.isoformat() if self.gifters else None,
            'total_gift_units': sum(x['gift_units'] for x in self.gifters.values()),
            'window_gift_units': sum(x['window_gift_units'] for x in self.gifters.values()),
            'unique_gifters': len(self.gifters), 'unknown_price_events': self.unknown_price_events,
            'top_gifters': [dict(x, rank=i) for i, x in enumerate(top, 1)],
        }


class Store:
    def __init__(self):
        project = _env('SUPABASE_PROJECT_ID')
        url = _env('SUPABASE_URL') or (f'https://{project}.supabase.co' if project else '')
        key = _env('SUPABASE_SECRET_KEY')
        if not url or not key:
            raise ValueError('Set SUPABASE_URL/PROJECT_ID and SUPABASE_SECRET_KEY in app.env')
        self.pending = {}
        self.lock = asyncio.Lock()
        self.client = httpx.AsyncClient(base_url=url.rstrip('/')+'/rest/v1/',
            headers={'apikey': key, 'Authorization': 'Bearer '+key}, timeout=30)

    async def check(self):
        response = await self.client.get(TABLE, params={'select': ','.join(Summary('check', '0', datetime.now(timezone.utc)).snapshot(1, datetime.now(timezone.utc))), 'limit': 0})
        if response.status_code in (404, 400):
            raise RuntimeError('Create tiktok_gifter_summaries using supabase/migrations/20261015_gifter_summaries.sql first')
        response.raise_for_status()

    async def save(self, summary, top_n):
        async with self.lock:
            self.pending[summary.id] = summary.snapshot(top_n, datetime.now(timezone.utc))
            # Retain failed compact snapshots across room changes, retry idempotently.
            for key, row in list(self.pending.items()):
                response = await self.client.post(TABLE, params={'on_conflict': 'observation_id'},
                    headers={'Prefer': 'resolution=merge-duplicates,return=minimal'}, json=row)
                response.raise_for_status()
                del self.pending[key]


async def listen(username, store, args, stop):
    summary = None
    client = TikTokLiveClient(unique_id=username)
    client.ignore_broken_payload = True  # Suppress library raw-payload error dumps.
    connected = False

    async def disconnect(close_client=False):
        try:
            await client.disconnect(close_client=close_client)
        except Exception as exc:
            LOG.warning('@%s disconnect failed (%s)', username, type(exc).__name__)

    async def flush():
        if summary is not None:
            await store.save(summary, args.top)

    @client.on(ConnectEvent)
    async def on_connect(event):
        nonlocal summary, connected
        now = datetime.now(timezone.utc)
        room = str(client.room_id)
        if summary is None or summary.room_id != room or summary.started.astimezone(TZ).date() != now.astimezone(TZ).date():
            previous = summary
            summary = Summary(username, room, now)
            if previous is not None:
                try:
                    await store.save(previous, args.top)
                except Exception as exc:
                    LOG.error('@%s previous summary queued for retry (%s)', username, type(exc).__name__)
        connected = True
        LOG.info('@%s connected; summaries only', username)

    @client.on(GiftEvent)
    async def on_gift(event):
        if summary is not None and (args.all_day or in_window(datetime.now(timezone.utc))):
            summary.add(event, datetime.now(timezone.utc))

    ticks = [0]

    async def periodic():
        while not stop.is_set():
            await asyncio.sleep(min(10, args.flush_seconds))
            ticks[0] += min(10, args.flush_seconds)
            if summary is not None and (connected or store.pending) and ticks[0] >= args.flush_seconds:
                ticks[0] = 0
                try:
                    await flush()
                except Exception as exc:
                    LOG.error('@%s summary flush failed (%s); retaining totals for retry', username, type(exc).__name__)
            if not args.all_day and not in_window(datetime.now(timezone.utc)):
                await disconnect()

    ticker = asyncio.create_task(periodic())
    try:
        while not stop.is_set():
            if args.all_day or in_window(datetime.now(timezone.utc)):
                try:
                    # Fetch the gift catalog so diamond_count is populated.
                    await client.connect(fetch_gift_info=True)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.warning('@%s connection ended (%s); retrying', username, type(exc).__name__)
                finally:
                    connected = False
                    await disconnect()
                    try:
                        await flush()
                    except Exception as exc:
                        LOG.error('@%s save failed (%s); totals retained in memory', username, type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=60+random.uniform(0,30))
            except asyncio.TimeoutError:
                pass
    finally:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
        await disconnect(close_client=True)
        for attempt in range(3):
            try:
                await flush()
                break
            except Exception as exc:
                LOG.error('@%s shutdown save failed (%s)', username, type(exc).__name__)
                await asyncio.sleep(attempt+1)


async def main(args):
    names = args.usernames or _env('GIFTER_SUMMARY_USERNAMES')
    if not names:
        raise ValueError('Set GIFTER_SUMMARY_USERNAMES or --usernames; no fallback to the full-event roster')
    usernames = list(dict.fromkeys(n.strip().lstrip('@').lower() for n in names.replace(',', ' ').split()))
    WebDefaults.tiktok_webcast_url = 'https://webcast.us.tiktok.com/webcast'
    if _env('EULERSTREAM_API_KEY'):
        WebDefaults.tiktok_sign_api_key = _env('EULERSTREAM_API_KEY')
    if _env('EULERSTREAM_SIGN_URL'):
        WebDefaults.tiktok_sign_url = _env('EULERSTREAM_SIGN_URL')
    store = Store()
    stop = asyncio.Event()
    tasks = []
    waiter = None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await store.check()
        if args.check:
            LOG.info('Summary table accessible; %d accounts configured', len(usernames))
            return
        tasks = [asyncio.create_task(listen(n, store, args, stop)) for n in usernames]
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait([*tasks, waiter], return_when=asyncio.FIRST_COMPLETED)
        stop.set()
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        waiter.cancel()
        for result in results:
            if isinstance(result, Exception):
                raise result
    finally:
        stop.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await store.client.aclose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--usernames', default='')
    parser.add_argument('--top', type=int, default=20)
    parser.add_argument('--flush-seconds', type=int, default=60)
    parser.add_argument('--all-day', action='store_true', help='Listen all day; still track Utah-window amounts separately')
    parser.add_argument('--check', action='store_true', help='Verify table access without connecting to TikTok')
    args = parser.parse_args()
    if not 1 <= args.top <= 100 or args.flush_seconds < 10:
        parser.error('--top must be 1..100; --flush-seconds must be >=10')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    try:
        asyncio.run(main(args))
    except (RuntimeError, ValueError) as exc:
        LOG.error('%s', exc)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        LOG.error('Supabase request failed (%s); check credentials, table setup, and connectivity', type(exc).__name__)
        raise SystemExit(1)
