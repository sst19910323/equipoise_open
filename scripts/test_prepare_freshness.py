"""Regression coverage for lunch freshness and feed metadata; no network or PG."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prepare_debate_data import _expected_latest_bar, _anchor_stale_minutes
from datalayer.fetchers.interactive_stock_fetcher import (
    MarketDataConfig, MarketDataFetcher, SymbolContract,
)


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


class FreshnessTest(unittest.TestCase):
    def test_tokyo_lunch_and_reopening(self):
        for clock, expected in [
            ('02:29', '02:29'), ('02:30', '02:30'),
            ('03:15', '02:30'), ('03:29', '02:30'), ('03:30', '03:30'),
            ('06:45', '06:30'),
        ]:
            with self.subTest(clock=clock):
                self.assertEqual(_expected_latest_bar('JP', utc('2026-10-29T' + clock)),
                                 utc('2026-10-29T' + expected))

    def test_previous_session_before_open_and_on_weekend(self):
        for clock in ['2026-10-28T23:30', '2026-11-01T03:15']:
            expected = '2026-10-28T06:30' if '28T' in clock else '2026-10-30T06:30'
            self.assertEqual(_expected_latest_bar('JP', utc(clock)), utc(expected))

    def test_lunch_does_not_accept_old_morning_data(self):
        class FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return utc('2026-10-29T03:15')
        config = {'data_sources': {'market_data_freshness': {'JP': 30}}}
        with patch('datetime.datetime', FixedClock):
            self.assertIsNone(_anchor_stale_minutes('JP', utc('2026-10-29T02:29'), config))
            self.assertEqual(_anchor_stale_minutes('JP', utc('2026-10-29T01:59'), config), 1)

    def test_korea_has_no_lunch_and_us_keeps_extended_hours(self):
        now = utc('2026-10-29T03:15')
        self.assertEqual(_expected_latest_bar('KR', now), now)
        for clock in ['2026-10-29T09:00', '2026-10-29T22:00']:
            self.assertEqual(_expected_latest_bar('US', utc(clock)), utc(clock))

    def test_delayed_feed_after_tokyo_lunch(self):
        class FixedClock(datetime):
            current = utc('2026-10-29T03:35')
            @classmethod
            def now(cls, tz=None):
                return cls.current
        config = {'data_sources': {'market_data_freshness': {'JP': 30}}}
        with patch('datetime.datetime', FixedClock):
            self.assertIsNone(_anchor_stale_minutes('JP', utc('2026-10-29T02:29'), config))
            self.assertEqual(_anchor_stale_minutes('JP', utc('2026-10-29T01:59'), config), 6)
            FixedClock.current = utc('2026-10-29T04:00')
            self.assertEqual(_anchor_stale_minutes('JP', utc('2026-10-29T02:29'), config), 1)
            self.assertIsNone(_anchor_stale_minutes('JP', utc('2026-10-29T03:40'), config))


class FeedMetadataTest(unittest.TestCase):
    def test_metadata_written_under_configured_symbol_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = MarketDataFetcher(MarketDataConfig(cache_dir=tmp, schedules=[]))
            for symbol, currency, mode, delay in [
                ('SHECY', 'JPY', 'delayed', 15), ('SPY', 'USD', 'realtime', 0),
            ]:
                with patch.object(fetcher, 'reqMarketDataType'):
                    fetcher.fetch_multi_resolutions(SymbolContract(symbol, currency=currency))
                meta = json.loads((Path(tmp) / symbol / '_feed_meta.json').read_text())
                self.assertEqual(meta['feed_mode'], mode)
                self.assertEqual(meta['feed_delay_minutes'], delay)
                self.assertTrue(meta['fetched_at'].endswith('Z'))


if __name__ == '__main__':
    unittest.main()
