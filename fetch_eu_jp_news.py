#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_eu_jp_news.py — 欧日韩新闻的**灌库器**(v3.3, 2026-10-26 起): 区域大盘(GENERAL_EU/JP/KR)与个股的新闻都由主会话的 agent 腿
/refresh-news <EU|JP|KR|SYM> 用自带搜索写成 staging, 本文件 --load 归一化后写 PG news(Finnhub 兼容格式)。
Linkup 区域搜索腿(--region 不带 --load)是上古 API 时期的残余(库主 10-25: 彻底不用, 逐步删; 见 docs/LEGACY-API-ERA.md), 备餐已不再调它。
输出格式与 Finnhub 兼容 (list of {id, headline, summary, source, url, datetime, category, related}),
存入 PG equipoise.news, ContextAssembler 自动识别。

搜源: **Linkup**(确定性腿, searchResults 直接给真实标题/链接/片段 → 映射成文章, 不经 LLM)。
      Linkup 没额度/挂了 → **agent 腿**: /refresh-news (CC 或 codex 自己联网搜, 共用一套业务
      prompt = .claude/commands/refresh-news.md), agent 写 staging 后用本文件 `--load` 灌库
      (归一化/去重/封顶在代码里, 不靠 prompt 自觉)。
      老的 Gemini grounding 封存在 _search_news_gemini (没免费额度了, 留着有钱再启)。
用法:
    python fetch_eu_jp_news.py [--region EU|JP|KR]                      # Linkup 确定性腿(区域)
    python fetch_eu_jp_news.py --load <staging.json> --region EU|JP|KR  # agent 腿灌库(区域)
    python fetch_eu_jp_news.py --load <staging.json> --symbol SIE       # agent 腿灌库(个股)
"""
import argparse
import hashlib
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
NEWS_LOOKBACK_DAYS = 3        # 只要近 N 天新闻 (Linkup from_date)
NEWS_MAX = 20                 # 每区取多少条 (2026-07-02 与 Finn 腿统一: 20条/3天)
NEWS_MAX_SYMBOL = 10          # 个股专属新闻(库主 2026-10-25): 十条左右就够——给辩手一个"大概发生了什么、该查什么"的轮廓,
                              # 捋前因后果是辩论里 task/Scout 的事, 不必一开场就刨根问底

# ── Linkup 搜索 query (新闻事件导向, 不堆 ticker 防搜成报价页; 一次搜一批) ──────
REGION_QUERIES = {
    "GENERAL_EU": ("欧洲", "latest European stock market news today: ECB rate policy, eurozone "
                          "inflation, DAX CAC, defense spending, semiconductor sector headlines"),
    "GENERAL_JP": ("日本", "latest Japan stock market news today: BOJ policy, yen, Nikkei 225, "
                          "semiconductors, defense, US-Japan trade headlines"),
    "GENERAL_KR": ("韩国", "latest South Korea stock market news today: Bank of Korea rate, won, "
                          "KOSPI, Samsung SK Hynix HBM memory, chip exports, defense export headlines"),
}
# 报价/概览聚合站 (永远"算新", 会把新闻搜成 ticker 页) → 排除
_EXCLUDE_DOMAINS = ["finance.yahoo.com", "stockanalysis.com", "seekingalpha.com",
                    "stocktwits.com", "tipranks.com", "marketbeat.com", "wallmine.com",
                    "investing.com", "tradingview.com", "barchart.com"]


def _make_id(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16)


def _domain(url: str) -> str:
    try:
        d = urlparse(url).netloc
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


def search_news(symbol: str, cfg: dict = None) -> list:
    """**Linkup** 搜区域新闻 → Finnhub 兼容 list。一次 Linkup 搜, 0 次 LLM。失败 → []。只认 GENERAL_* 键。"""
    region_label, query = REGION_QUERIES[symbol]
    log.info(f"[{symbol}] Linkup 搜{region_label}新闻...")
    from workflows.linkup_search import search_one
    from_date = (datetime.now(timezone.utc) - timedelta(days=NEWS_LOOKBACK_DAYS)).date()
    results = search_one(query, from_date=from_date, max_results=NEWS_MAX,
                         exclude_domains=_EXCLUDE_DOMAINS)

    now_ts = int(datetime.now(timezone.utc).timestamp())
    out, seen = [], set()
    for i, r in enumerate(results):
        headline = (r.get("name") or "").strip() or (r.get("content") or "")[:80].strip()
        url = (r.get("url") or "").strip()
        key = url or headline
        if not headline or key in seen:
            continue
        seen.add(key)
        out.append({
            "id":       _make_id(key),
            "category": "business",
            "datetime": now_ts - i * 60,          # 降序保顺序; 精确发布日不强求(近N天窗已保证新)
            "headline": headline,
            "summary":  (r.get("content") or "").strip()[:500],
            "source":   _domain(url) or "linkup",
            "url":      url,
            "image":    "",
            "related":  symbol,
        })
    log.info(f"[{symbol}] Linkup 搜完, {len(out)} 条新闻")
    return out


def save_news(symbol: str, items: list, news_cfg: dict = None):
    """欧日新闻写入 PG equipoise.news (fetched_at=now); 不落 JSON。"""
    from datalayer.pg.marketdata_pg_store import save_news as pg_save_news
    try:
        ok = pg_save_news(symbol, items, datetime.now(timezone.utc))
        log.info(f"[{symbol}] {len(items)} 条新闻 → PG news ({'ok' if ok else 'failed'})")
    except Exception as e:
        log.warning(f"[{symbol}] PG 写 news 异常(忽略): {e}")
    return None


def run(regions: list):
    for symbol in regions:
        items = search_news(symbol)
        if items:
            save_news(symbol, items)
        else:
            log.warning(f"[{symbol}] 无新闻写入")
        time.sleep(2)


def load_from_file(path: str, symbol: str) -> int:
    """--load 模式: 读 agent(/refresh-news) 写的 staging items → Finn 兼容归一化 → PG news。
    归一化/去重/排除报价站/封顶 NEWS_MAX 全在这, 不靠 agent 的 prompt 自觉。返回入库条数。"""
    import json
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = data.get("items") if isinstance(data, dict) else data
    now_ts = int(datetime.now(timezone.utc).timestamp())
    out, seen = [], set()
    for i, r in enumerate(raw or []):
        if not isinstance(r, dict):
            continue
        headline = str(r.get("headline") or "").strip()
        url = str(r.get("url") or "").strip()
        key = url or headline
        if not headline or not url or key in seen or _domain(url) in _EXCLUDE_DOMAINS:
            continue
        seen.add(key)
        ts = now_ts - i * 60
        pub = str(r.get("published") or "").strip()
        if pub:
            try:
                ts = int(datetime.strptime(pub[:10], "%Y-%m-%d")
                         .replace(hour=12, tzinfo=timezone.utc).timestamp()) - i
            except ValueError:
                pass
        out.append({
            "id":       _make_id(key),
            "category": "business",
            "datetime": ts,
            "headline": headline,
            "summary":  str(r.get("summary") or "").strip()[:500],
            "source":   str(r.get("source") or "").strip() or _domain(url) or "agent",
            "url":      url,
            "image":    "",
            "related":  symbol,
        })
        if len(out) >= (NEWS_MAX if symbol.startswith("GENERAL_") else NEWS_MAX_SYMBOL):
            break
    if out:
        save_news(symbol, out)
    print(f"[news_load] {symbol}: {len(out)} items → PG news (raw {len(raw or [])})")
    return len(out)


# ═══════════════════════════════════════════════════════════════════════════
# 【封存】Gemini grounding 版 (没免费额度了, 留着有钱再启; 当前不调用)
# ═══════════════════════════════════════════════════════════════════════════
_GEMINI_PROMPTS = {
    "GENERAL_EU": "你是欧洲金融新闻情报官。用 Google 搜过去48小时最重要的欧洲财经新闻(ECB/DAX/CAC/AEX/"
                  "FTSE/Rheinmetall/ASML/BESI/地缘)。每条:标题+摘要(<100字)+来源+URL。优先英文权威源, 中文源标注。"
                  "输出 JSON 数组 [{headline,summary,source,url,category}], 15-25 条, 只输出 JSON。",
    "GENERAL_JP": "你是日本金融新闻情报官。用 Google 搜过去48小时最重要的日本财经新闻(BOJ/日元/N225/TOPIX/"
                  "任天堂7974/三菱重工7011/信越4063/半导体/防卫/美日)。每条:标题+摘要(<100字)+来源+URL。"
                  "优先英文权威源, 日文/中文源标注。输出 JSON 数组, 15-25 条, 只输出 JSON。",
}


def _search_news_gemini(symbol: str) -> list:
    """封存: Gemini grounding 拉新闻。provider 想切回 gemini 时启用。"""
    import json
    import re
    import yaml
    from google import genai
    from google.genai import types, errors as genai_errors
    with open(ROOT / "config" / "models.yaml", encoding="utf-8") as f:
        api_key = yaml.safe_load(f)["models"]["gemini"]["api_key"]
    client = genai.Client(api_key=api_key)
    resp = None
    for attempt in range(1, 5):
        try:
            resp = client.models.generate_content(
                model="gemini-3.5-flash", contents=_GEMINI_PROMPTS[symbol],
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())], temperature=0.1))
            break
        except genai_errors.ServerError:
            time.sleep(2 ** attempt)
    if resp is None:
        return []
    raw = resp.text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
    if m:
        raw = m.group(1).strip()
    s, e = raw.find("["), raw.rfind("]")
    if s != -1 and e > s:
        raw = raw[s:e + 1]
    try:
        items = json.loads(raw)
    except Exception:
        return []
    now_ts = int(datetime.now(timezone.utc).timestamp())
    out = []
    for i, it in enumerate(items if isinstance(items, list) else []):
        h = str(it.get("headline") or it.get("title") or "").strip()
        if not h:
            continue
        out.append({"id": _make_id(h), "category": it.get("category", "business"),
                    "datetime": now_ts - i * 60, "headline": h,
                    "summary": str(it.get("summary") or "").strip(),
                    "source": str(it.get("source") or "Gemini/Google").strip(),
                    "url": str(it.get("url") or "").strip(), "image": "", "related": symbol})
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", choices=["EU", "JP", "KR"], help="只跑指定 region 的大盘新闻")
    parser.add_argument("--symbol", help="配 --load: 把 agent 腿(/refresh-news <SYM>)写的 staging 灌到该个股的 news 行")
    parser.add_argument("--load", metavar="STAGING_JSON",
                        help="agent 腿灌库模式: 读 staging items 归一化后写 PG (须配 --region 或 --symbol)")
    args = parser.parse_args()
    if args.load:
        if not args.region and not args.symbol:
            parser.error("--load 必须配 --region EU|JP|KR 或 --symbol <SYM>")
        target = args.symbol.upper() if args.symbol else f"GENERAL_{args.region}"
        raise SystemExit(0 if load_from_file(args.load, target) > 0 else 1)
    if args.symbol:
        parser.error("--symbol 只配 --load 用(个股新闻走 agent 腿 /refresh-news <SYM>, 不走 Linkup)")
    targets = {"EU": ["GENERAL_EU"], "JP": ["GENERAL_JP"], "KR": ["GENERAL_KR"]}.get(
        args.region, ["GENERAL_EU", "GENERAL_JP", "GENERAL_KR"])
    run(targets)
