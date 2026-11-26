"""Regression fixtures from failed AJINY/AMD/AAPL/ITA postcheck preparations.

Only original report execution fields and deterministic bars are retained. No PG,
network, or model judgment is used; these are not fresh outcome requests.
"""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from workflows import postcheck_quant as pq
from workflows.postcheck_execution import contract, settle_exit, normalize_prices, apply_settlement

FIXTURES = Path(__file__).parent / 'fixtures' / 'postcheck_execution'


def fixture(symbol):
    return json.loads((FIXTURES / f'{symbol}.json').read_text(encoding='utf-8'))


def execution(f):
    r = f['report']
    return contract(r, pq._to_dt(r['market_data_timestamp_utc']), pq._stop_price(r, ref=f['price_T']), pq._stop_rule(r))


class ExecutionRegression(unittest.TestCase):
    def test_ajiny_two_closes_reset_on_reclaim(self):
        f = fixture('AJINY'); e = execution(f)
        self.assertEqual(e['stop']['count'], 2)
        self.assertEqual(e['stop']['level'], 5030)
        self.assertFalse(e['issues'])
        result = settle_exit(f['bars'], 0, f['price_T'], e['stop'])
        self.assertEqual(result['exit_date'], '2026-11-05')
        self.assertEqual(result['trigger_date'], '2026-11-05')

    def test_amd_no_sale_and_explicit_cancel_date(self):
        f = fixture('AMD'); e = execution(f)
        self.assertIsNone(pq._stop_price(f['report'], entry=596, ref=612.57))
        self.assertIsNone(e['stop'])
        self.assertEqual(e['valid_until'], '2026-10-16')
        self.assertEqual(pq._order_valid_until(f['report'], datetime(2026, 10, 9)), '2026-10-16')
        self.assertTrue(e['issues'])  # Partial sale is not a full stop or hold-to-end return.

    def test_aapl_review_exception_is_not_assumed_away(self):
        f = fixture('AAPL'); e = execution(f)
        self.assertEqual(e['exit_deadline']['date'], '2026-10-30')
        result = settle_exit(f['bars'], 0, f['price_T'], e['stop'], issues=e['issues'])
        self.assertEqual(result['settlement_status'], 'unassessable')
        self.assertIsNone(result['simulated_exit_ret_pct'])
        d = {'daily_bars': f['bars'], 'price_T': f['price_T'],
             'alternatives': pq.immediate_action_stats(f['bars'], f['price_T'], stop=328)}
        apply_settlement(d, f['report'], e)
        self.assertIsNone(d['actual_execution']['realized_ret_pct'])
        self.assertIsNone(d['alternatives']['no_action']['opportunity_cost_pct'])
        self.assertIsNone(d['alternatives']['immediate_trim']['vs_keep_pct'])

    def test_next_open_pending_and_gap_fill(self):
        stop = {'level': 95, 'semantics': 'close', 'count': 1, 'fill': 'next_open'}
        bars = [{'date': '2026-11-05', 'open': 97, 'high': 99, 'low': 93, 'close': 94}]
        r = settle_exit(bars, 0, 100, stop)
        self.assertEqual(r['settlement_status'], 'pending_execution')
        self.assertIsNone(r['simulated_exit_ret_pct'])
        bars.append({'date': '2026-11-06', 'open': 90, 'high': 96, 'low': 89, 'close': 95})
        r = settle_exit(bars, 0, 100, stop)
        self.assertEqual((r['exit_date'], r['exit_price'], r['simulated_exit_ret_pct']), ('2026-11-06', 90, -10))

    def test_ita_full_digest_normalizes_before_all_calculation(self):
        import pandas as pd
        f = fixture('ITA'); original = copy.deepcopy(f['report'])
        df = pd.DataFrame(f['current_daily'])
        df.columns = [k.lower() for k in df.columns]
        date_col = next(k for k in ('date', 'datetime', 'time') if k in df)
        df[date_col] = pd.to_datetime(df[date_col], utc=True)
        df = df.set_index(date_col)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / f['report']['data_files']['symbol']['technical']
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(f['snapshot']), encoding='utf-8')
            with patch.object(pq, 'PROJECT_ROOT', root), patch.object(pq, '_load_daily_df', side_effect=lambda *a, **kw: None if kw.get('res') == '5m' else df):
                d = pq.compute_window_digest('ITA', '20261102T152600Z', 3, f['report'])
        self.assertEqual(f['report'], original)
        self.assertAlmostEqual(d['price_normalization']['factor'], 3, places=4)
        self.assertGreater(d['daily_bars'][0]['close'], 200)
        self.assertGreater(d['ret_pct'], -10)
        a = d['alternatives']['immediate_entry']
        self.assertEqual(a['trigger_date'], '2026-11-05')
        self.assertEqual(a['settlement_status'], 'pending_execution')
        self.assertIsNone(a['realized_ret_pct'])
        self.assertEqual(d['actual_execution'], a)

    def test_invalid_structured_stop_does_not_fall_back(self):
        e = contract({'operation_execution': {'stop': {'level': 'bad'}},
                      'risk_management': {'stop_loss': 'sell below 95'}}, None, 95)
        self.assertIsNone(e['stop'])
        self.assertTrue(e['issues'])

    def test_review_only_and_explicit_no_stop_are_not_sales(self):
        for report in [
            {'risk_management': {'stop_loss': '95 close below triggers review only'}},
            {'operation_execution': {'stop': None}, 'risk_management': {'stop_loss': 'sell below 95'}}
        ]:
            self.assertIsNone(contract(report, None, 95)['stop'])

    def test_unparsed_expiry_is_flagged(self):
        r = {'operation_type': 'LIMIT_ENTRY', 'statement': 'Cancel the order before the next earnings release.'}
        e = contract(r, datetime(2026, 11, 2, tzinfo=timezone.utc))
        self.assertTrue(e['expiry_instruction_present'])
        self.assertIsNone(e['valid_until'])

    def test_compound_exit_is_not_reduced_to_price_only(self):
        r = {'risk_management': {'stop_loss': '收盘低于95且放量即退出'}}
        self.assertTrue(contract(r, None, 95)['issues'])

    def test_short_side_and_consecutive_reset(self):
        stop = {'level': 105, 'semantics': 'close', 'count': 2, 'fill': 'next_open'}
        bars = [{'date': f'2026-11-{i+2:02}', 'open': c, 'high': c+1, 'low': c-1, 'close': c}
                for i, c in enumerate([106, 104, 106, 107, 110])]
        r = settle_exit(bars, 0, 100, stop, long_side=False)
        self.assertEqual(r['trigger_date'], '2026-11-05')
        self.assertEqual(r['exit_price'], 110)


if __name__ == '__main__':
    unittest.main()
