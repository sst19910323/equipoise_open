"""Conservative execution interpretation. Unresolved prose is never a trade.

This module does not rewrite reports. Each executable constraint retains its source;
unquantified review, conditional exits and partial sales remain explicit limitations.
"""
from __future__ import annotations

import re
from datetime import date

DATE = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}日)"
EXIT = r"退出|撤去|卖出|减仓|止损|\bexit\b|\bsell\b|\bstop\b"
REVIEW = r"重审|重评|暂停新增|停买|停止新增|\breassess|\breview"
NEGATIVE = r"不自动(?:止损)?卖出|不是.*止损|不自动止损|not an? automatic (?:sale|exit)|no (?:automatic |standing )?(?:stop|sale)"


def texts(report):
    rm, nr = report.get('risk_management') or {}, report.get('next_review') or {}
    return [str(x) for x in [report.get('statement'), report.get('mentality_basis'),
            rm.get('stop_loss'), rm.get('take_profit'), nr.get('notes'),
            *(rm.get('review_triggers') or [])] if x]


def iso_date(raw, as_of):
    if '-' in raw:
        return date.fromisoformat(raw).isoformat()
    month, day = map(int, re.findall(r'\d+', raw))
    if as_of is None:
        raise ValueError('year absent without report timestamp')
    return date(as_of.year, month, day).isoformat()


def consecutive(text):
    nword = r'(\d+|两个|两|二|三|双|two|three)'
    patterns = [r'连续\s*' + nword + r'[^。；;，,]{0,12}收盘',
                nword + r'\s*(?:个)?(?:日本|常规盘?|正规|交易日|daily|regular[- ]session|completed|consecutive|\s)*收盘',
                nword + r'\s+(?:consecutive\s+)?(?:(?:completed|daily|regular[- ]session)\s+)*closes?',
                r'(\d+)\s*(?:日|days?)\s*(?:未收回|without reclaim)']
    values = {'两个': 2, '两': 2, '二': 2, '双': 2, '三': 3, 'two': 2, 'three': 3}
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            word = m.group(1).lower()
            return values[word] if word in values else int(word)
    if re.search(r'次日未收回|next (?:trading )?day.*not reclaim', text, re.I):
        return 2
    return 1


def dated_expiry(report, as_of):
    for text in texts(report):
        for clause in re.split(r'[。；;]', text):
            # Date first: do not bind the previous event date in the same sentence.
            for m in re.finditer(DATE, clause):
                tail = clause[m.end():]
                next_date = re.search(DATE, tail)
                if next_date:
                    tail = tail[:next_date.start()]
                if re.search(r'(?<!\d)\d{1,2}日', tail):
                    continue  # An abbreviated later date must not inherit the earlier event date.
                if re.search(r'(?:未成交|未成单|unfilled).*?(?:撤单|到期|取消|cancel|expir)', tail, re.I):
                    return iso_date(m.group(), as_of)
    return None


def contract(report, as_of, legacy_price=None, legacy_rule=('touch', 1)):
    ex = report.get('operation_execution')
    ex = ex if isinstance(ex, dict) else {}
    raw = str((report.get('risk_management') or {}).get('stop_loss') or '')
    out = {'version': 'execution-v1', 'stop': None, 'issues': [], 'review_only': [],
           'source_stop_text': raw, 'exit_deadline': None}
    structured = ex.get('stop')
    if isinstance(structured, dict):
        level, semantics = structured.get('level'), structured.get('semantics')
        if isinstance(level, (int, float)) and level > 0 and semantics in ('touch', 'close'):
            out['stop'] = {'level': float(level), 'semantics': semantics,
                           'count': structured.get('count', consecutive(raw)),
                           'fill': structured.get('fill', 'next_open' if semantics == 'close' and
                               (report.get('odds') or {}).get('stop_fill_without_open') == 'next_price' else 'at_level'),
                           'source': 'operation_execution.stop'}
        else:
            out['issues'].append('invalid structured stop; prose fallback forbidden')
    elif 'stop' in ex:
        pass  # An explicitly empty derived stop is not replaced with a number in prose.
    else:
        clauses = [s.strip() for s in re.split(r'[。；;]', raw) if s.strip()]
        candidates = []
        for clause in clauses:
            if re.search(NEGATIVE, clause, re.I) or (re.search(REVIEW, clause, re.I) and
                    not re.search(r'退出|撤去|卖出|减仓|\bexit\b|\bsell\b', clause, re.I)):
                out['review_only'].append(clause)
            elif re.search(EXIT, clause, re.I) or re.fullmatch(r'\s*\d+(?:\.\d+)?\s*', clause):
                candidates.append(clause)
        if len(candidates) == 1:
            clause = candidates[0]
            extra = re.sub(r'且次日未收回', '', clause)
            if re.search(r'且|并且|同时|缩量|放量|企稳|守稳|\bunless\b|\bvolume\b', extra, re.I):
                out['issues'].append('exit has an additional condition not settled by daily price bars')
            # Price extraction is scoped to the action-bearing clause, never the full paragraph.
            numbers = [float(x) for x in re.findall(r'(?<![\d.])\d+(?:\.\d+)?',
                re.sub(r'\d+\s*(?:日|天|days?|sessions?)', '', clause, flags=re.I))]
            price = legacy_price if legacy_price in numbers else (numbers[0] if numbers else None)
            if price:
                sem = 'close' if re.search(r'收盘|收回|\bcloses?\b|closing', clause, re.I) else legacy_rule[0]
                out['stop'] = {'level': price, 'semantics': sem, 'count': consecutive(clause),
                               'fill': 'next_open' if re.search(r'次日.*开盘|next.*open', clause, re.I) else 'at_level',
                               'source': clause}
            else:
                out['issues'].append('exit instruction has no calculable price')
        elif candidates:
            out['issues'].append('multiple exit clauses require explicit settlement')
        elif raw and not out['review_only']:
            out['issues'].append('stop instruction is not mechanically interpretable')
    stop = out['stop']
    if stop:
        if re.search(r'次日.*开盘|next.*open', raw, re.I):
            stop['fill'] = 'next_open'
        if stop['fill'] not in ('next_open', 'at_level') or not isinstance(stop['count'], int) or stop['count'] < 1:
            out['issues'].append('unsupported stop execution timing or count')
        if re.search(NEGATIVE, raw, re.I) and isinstance(structured, dict):
            out['issues'].append('structured stop conflicts with explicit no-sale text')
    for text in texts(report):
        for clause in re.split(r'[。]', text):
            if not re.search(r'撤去本次新增|退出.*(?:新增|仓位)|exit.*(?:position|sleeve)', clause, re.I):
                continue
            m = re.search(DATE, clause)
            if m:
                out['exit_deadline'] = {'date': iso_date(m.group(), as_of), 'source': clause}
                # A review is a decision, not a price predicate. Daily OHLC cannot prove it happened.
                if re.search(r'重审|有效判断|unless|review', clause, re.I):
                    out['issues'].append('dated exit depends on an unobserved review; cannot assume hold through window end')
                if not re.search(r'收盘|close', clause, re.I):
                    out['issues'].append('dated exit has no exact executable time')
    profit = str((report.get('risk_management') or {}).get('take_profit') or '')
    if re.search(r'分批兑现|分批止盈|partial.*(?:sell|profit)', profit, re.I):
        out['issues'].append('partial profit-taking size/timing is not calculable from OHLC')
    out['valid_until'] = ex.get('valid_until') or dated_expiry(report, as_of)
    out['valid_days'] = ex.get('valid_days')
    if not out['valid_until'] and report.get('operation_type') in ('LIMIT_ENTRY', 'ENTRY'):
        # The caller may recognize the older documented date/day grammar. This
        # flag is resolved there, never by silently applying the three-day default.
        out['expiry_instruction_present'] = any(re.search(r'撤单|到期|有效|cancel|expir|valid', t, re.I) for t in texts(report))
    out['issues'] = list(dict.fromkeys(out['issues']))
    return out


def apply_settlement(digest, report, execution):
    """Actual and alternative entries share the exact same exit contract."""
    bars, price = digest.get('daily_bars') or [], digest.get('price_T')
    if not bars or not price:
        return
    stop, issues = execution['stop'], execution['issues']
    def result(start, fill, alternative=False):
        protective = None if alternative and stop and stop['level'] >= fill else stop
        return settle_exit(bars, start, fill, protective, issues=issues, deadline=execution['exit_deadline'])
    alt = digest['alternatives']['immediate_entry']
    for key in list(alt):
        if key.startswith(('exit_', 'simulated_')) or key == 'stop_not_applicable':
            alt.pop(key)
    alt.update(result(0, price, alternative=True))
    alt['realized_ret_pct'] = alt.get('simulated_exit_ret_pct')
    digest['alternatives']['no_action']['opportunity_cost_pct'] = alt['realized_ret_pct']
    trim = digest['alternatives'].get('immediate_trim')
    if trim is not None:
        trim['vs_keep_pct'] = -alt['realized_ret_pct'] if alt['realized_ret_pct'] is not None else None
        trim['basis'] = 'vs entering/holding with the same execution contract'
    ent = digest.get('entry')
    if ent and ent.get('entry_price_reached'):
        start = next((i for i, b in enumerate(bars) if str(b['date'])[:10] == ent.get('reached_date')), None)
        if start is not None:
            for key in list(ent):
                if key.startswith(('exit_', 'simulated_')):
                    ent.pop(key)
            ent.update(result(start, ent.get('fill_price', ent['entry_price'])))
    if report.get('operation_type') == 'MARKET_ENTRY' and not ent:
        digest['actual_execution'] = dict(alt)
    elif ent:
        digest['actual_execution'] = dict(ent)
    digest['execution_contract'] = execution
    digest['stop_rule'] = (f"{stop['semantics']} x{stop['count']}" if stop else 'none')
    digest['settlement_status'] = 'unassessable' if issues else 'interpreted'
    if issues:
        digest['unassessable_reasons'] = list(issues)


def normalize_prices(df, report, symbol, root):
    """Reconcile current OHLC to frozen report units using same-date historical OHLC.

    No ratio is taken from narrative or inferred from a crash. At least three matched
    sessions and all four OHLC ratios must agree; original report stays unchanged.
    """
    import json
    import statistics
    from pathlib import Path
    paths = []
    def walk(obj):
        if isinstance(obj, dict):
            for value in obj.values():
                walk(value)
        elif isinstance(obj, str) and 'technical' in obj and obj.endswith('.json'):
            paths.append(obj)
    walk(report.get('data_files') or {})
    for ref in report.get('reference_prices') or []:
        if isinstance(ref, dict) and ref.get('symbol') == symbol and ref.get('source'):
            paths.append(ref['source'])
    by_date = {str(idx)[:10]: row for idx, row in df.iterrows()}
    for raw_path in dict.fromkeys(paths):
        path = Path(raw_path)
        if not path.is_absolute():
            path = root / path
        try:
            snap = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        rows = snap.get(f'{symbol}_price_digest_daily') or []
        ratios, matches = [], []
        for row in rows:
            period = str(row[0]).split('..')
            if len(period) != 2 or period[0] != period[1] or period[0] not in by_date:
                continue
            current = by_date[period[0]]
            rs = [float(row[i]) / float(current[k]) for i, k in enumerate(('open', 'high', 'low', 'close'), 1)
                  if k in current and float(current[k]) > 0]
            if len(rs) == 4:
                ratios.extend(rs)
                matches.append(period[0])
        if len(matches) < 3:
            continue
        factor = statistics.median(ratios)
        if max(abs(r / factor - 1) for r in ratios) > 0.002:
            return df, {'status': 'unassessable', 'reason': 'frozen/current OHLC ratios disagree', 'source': str(path)}
        if abs(factor - 1) < 0.002:
            return df, {'status': 'same_scale', 'factor': 1.0, 'source': str(path)}
        scaled = df.copy()
        for key in ('open', 'high', 'low', 'close'):
            if key in scaled:
                scaled[key] = scaled[key] * factor
        return scaled, {'status': 'normalized', 'factor': factor, 'basis': 'frozen report price scale',
                        'source': str(path), 'matched_dates': matches,
                        'report_unchanged': True,
                        'attribution_note': 'Measured historical price-unit ratio, not validation of any split ratio claimed in the report.'}
    return df, {'status': 'unverified', 'reason': 'no matching frozen daily OHLC evidence'}


def settle_exit(bars, start, price, stop, long_side=True, issues=(), deadline=None):
    if issues:
        return {'settlement_status': 'unassessable', 'unassessable_reasons': list(issues),
                'simulated_exit_ret_pct': None, 'exit_reason': 'unassessable', 'exit_date': None}
    if not bars or start >= len(bars):
        return {'settlement_status': 'unassessable', 'simulated_exit_ret_pct': None, 'exit_reason': 'no_bars'}
    run = 0
    for i in range(start, len(bars)):
        b = bars[i]
        day = str(b['date'])[:10]
        hit, reason = False, None
        if stop:
            level = stop['level']
            if stop['semantics'] == 'close':
                beyond = b['close'] < level if long_side else b['close'] > level
                run = run + 1 if beyond else 0
                hit = run >= stop['count']
                reason = 'stop_close'
            else:
                hit = b['low'] <= level if long_side else b['high'] >= level
                reason = 'stop'
        if deadline and day >= deadline['date']:
            hit, reason = True, 'dated_exit'
        if not hit:
            continue
        trigger_date = day
        if reason != 'dated_exit' and stop['fill'] == 'next_open':
            if i + 1 >= len(bars) or bars[i + 1].get('open') is None:
                return {'settlement_status': 'pending_execution', 'trigger_date': day,
                        'exit_date': None, 'exit_reason': 'next_open_pending', 'simulated_exit_ret_pct': None}
            i += 1
            px = bars[i]['open']
            day = str(bars[i]['date'])[:10]
        elif reason in ('stop_close', 'dated_exit'):
            px = b['close']
        else:
            px = min(b.get('open', level), level) if long_side else max(b.get('open', level), level)
        ret = (px / price - 1) * 100 if long_side else (price / px - 1) * 100
        return {'settlement_status': 'settled', 'trigger_date': trigger_date, 'exit_date': day,
                'exit_price': px, 'exit_reason': reason, 'simulated_exit_ret_pct': round(ret, 2),
                **({'exit_same_bar_as_fill': True} if reason == 'stop' and i == start else {})}
    px = bars[-1]['close']
    ret = (px / price - 1) * 100 if long_side else (price / px - 1) * 100
    return {'settlement_status': 'marked_to_window_end', 'exit_date': str(bars[-1]['date'])[:10],
            'exit_reason': 'window_end', 'simulated_exit_ret_pct': round(ret, 2)}
