# -*- coding: utf-8 -*-
"""期权做市商定位剖面 (dealer gamma): GEX / ZGL / 双墙 / ATM 隐含波幅。

数据源: CBOE 免费延迟链 (cdn.cboe.com, 无鉴权, gamma/IV/OI 现成)。OI 日更,
延迟数据零损失; 开盘前备餐拉一次即可, 盘中刷新无意义。
口径: 标准 SpotGamma 式 dealer 方向假设(卖 call 买 put)——
    GEX_K = (call_OI×Γc − put_OI×Γp) × 100 × S² × 0.01   ($ 每 1% 现价变动)
ZGL 用各合约 IV 重算 BS gamma 在现价 ±10% 网格上扫符号翻转; 附 IV ±10% 扰动带。
净 vanna(每 1 IV 点)同链 BS 求得, 只报符号+量级(事件日 IV 塌陷方向推力的证据)。

定位: 只服务市况分类与挂点层(正 gamma=均值回归机制在场, 负 gamma=趋势易加速),
不参与多空判断。dealer 方向假设在个股上常不成立 → 只对 SPY/QQQ 计算。
"""
from __future__ import annotations

import json
import math
import re
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

OCC = re.compile(r"^(\w+?)\s*(\d{6})([CP])(\d{8})$")
RISK_FREE = 0.04
MULT = 100

_ALLOWED = {"SPY", "QQQ"}   # dealer 方向假设只在宽基 ETF 上站得住


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1(S: float, K: float, T: float, iv: float) -> float:
    return (math.log(S / K) + (RISK_FREE + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))


def _bs_gamma(S: float, K: float, T: float, iv: float) -> float:
    if T <= 0 or iv <= 0 or S <= 0:
        return 0.0
    return _norm_pdf(_d1(S, K, T, iv)) / (S * iv * math.sqrt(T))


def _bs_vanna(S: float, K: float, T: float, iv: float) -> float:
    """dVega/dSpot ≈ dDelta/dVol, 每 1.00 IV; 调用侧换算成每 1 IV 点(0.01)。"""
    if T <= 0 or iv <= 0 or S <= 0:
        return 0.0
    d1 = _d1(S, K, T, iv)
    d2 = d1 - iv * math.sqrt(T)
    return -_norm_pdf(d1) * d2 / iv


def _fetch_chain(symbol: str, timeout: int = 30) -> dict:
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def build_dealer_gamma(symbol: str, now_utc: datetime = None) -> dict:
    """拉链+计算, 返回 {ok, ..., trigger_signal}; 任何失败 {ok: False, error}。"""
    symbol = symbol.upper()
    if symbol not in _ALLOWED:
        return {"ok": False, "symbol": symbol,
                "error": f"dealer 方向假设不适用({symbol}), 只算 {sorted(_ALLOWED)}"}
    try:
        js = _fetch_chain(symbol)
        d = js["data"]
        S = d.get("current_price") or d.get("close")
        today = (now_utc or datetime.now(timezone.utc)).date()
        rows = []                       # (K, T, iv, oi, sign) — 供网格重算
        per_strike = defaultdict(float)
        tot_gex = tot_vanna = 0.0
        for o in d.get("options", []):
            m = OCC.match(o.get("option", ""))
            if not m:
                continue
            _, ymd, cp, kk = m.groups()
            K = int(kk) / 1000.0
            T = max((datetime.strptime(ymd, "%y%m%d").date() - today).days, 0) / 365.0
            iv = o.get("iv") or 0.0
            oi = o.get("open_interest") or 0
            g = o.get("gamma") or 0.0
            if not oi:
                continue
            sign = 1.0 if cp == "C" else -1.0
            per_strike[K] += sign * oi * g * MULT * S * S * 0.01
            tot_gex += sign * oi * g * MULT * S * S * 0.01
            tot_vanna += sign * oi * _bs_vanna(S, K, T, iv) * MULT * 0.01 * S
            rows.append((K, T, iv, oi, sign))
        if not rows:
            return {"ok": False, "symbol": symbol, "error": "链为空或全零 OI"}

        call_wall = max(per_strike, key=lambda k: per_strike[k])
        put_wall = min(per_strike, key=lambda k: per_strike[k])

        def _zgl(iv_scale: float):
            prev_sp = prev_v = None
            for i in range(101):
                sp = S * (0.90 + 0.002 * i)
                v = sum(sg * oi * _bs_gamma(sp, K, T, iv * iv_scale) * MULT * sp * sp * 0.01
                        for K, T, iv, oi, sg in rows if T > 0)
                if prev_v is not None and prev_v < 0 <= v:
                    return prev_sp + (sp - prev_sp) * (-prev_v) / (v - prev_v)
                prev_sp, prev_v = sp, v
            return None

        zgl = _zgl(1.0)
        zgl_band = [z for z in (_zgl(0.9), _zgl(1.1)) if z]

        fwd = sorted({(T, K, iv) for K, T, iv, oi, s in rows if T > 0})
        near_t = fwd[0][0] if fwd else 0
        atm = [iv for T, K, iv in fwd
               if abs(T - near_t) < 1e-9 and abs(K - S) / S < 0.01 and iv > 0]
        atm_iv = sum(atm) / len(atm) if atm else None
        mv1 = atm_iv * math.sqrt(1 / 252) * 100 if atm_iv else None
        mv3 = atm_iv * math.sqrt(3 / 252) * 100 if atm_iv else None

        pos = tot_gex > 0
        out = {
            "ok": True, "symbol": symbol, "spot_delayed": round(S, 2),
            "total_gex_bn_per_1pct": round(tot_gex / 1e9, 2),
            "regime": "positive" if pos else "negative",
            "zgl": round(zgl, 1) if zgl else None,
            "zgl_band_iv10pct": [round(z, 1) for z in zgl_band],
            "call_wall": call_wall,
            "call_wall_gex_bn": round(per_strike[call_wall] / 1e9, 2),
            "put_wall": put_wall,
            "put_wall_gex_bn": round(per_strike[put_wall] / 1e9, 2),
            "atm_iv_nearest_exp": round(atm_iv, 3) if atm_iv else None,
            "exp_move_1d_pct": round(mv1, 2) if mv1 else None,
            "exp_move_3d_pct": round(mv3, 2) if mv3 else None,
            "net_vanna_bn_per_ivpt": round(tot_vanna / 1e9, 2),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        out["trigger_signal"] = (
            f"{symbol} dealer gamma {'+' if pos else '−'}${abs(out['total_gex_bn_per_1pct'])}bn/1%"
            f"({'正: 做市商对冲压波动, 均值回归机制在场' if pos else '负: 对冲追趋势, 波动易放大'})"
            f" · ZGL {out['zgl']}"
            + (f"(IV±10%扰动 {min(zgl_band):.0f}-{max(zgl_band):.0f})" if len(zgl_band) > 1 else "")
            + f" · call wall {call_wall:g} / put wall {put_wall:g}"
            f" · ATM IV {out['atm_iv_nearest_exp']:.1%} → 1日±{mv1:.2f}%/3日±{mv3:.2f}%"
            f" · 净vanna {'+' if tot_vanna > 0 else '−'}${abs(out['net_vanna_bn_per_ivpt'])}bn/IV点"
            f" · 口径: SpotGamma式 OI 假设, 日更, 参考非开关"
        )
        return out
    except Exception as e:
        return {"ok": False, "symbol": symbol, "error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    import sys
    for sym in sys.argv[1:] or ["SPY", "QQQ", "AAPL"]:
        r = build_dealer_gamma(sym)
        if r.get("ok"):
            print(r["trigger_signal"])
            assert r["regime"] in ("positive", "negative")
            assert r["call_wall"] > r["put_wall"], "双墙次序异常"
            assert r["exp_move_3d_pct"] > r["exp_move_1d_pct"] > 0
        else:
            print(f"{sym}: SKIP — {r['error']}")
    print("selftest-ok")
