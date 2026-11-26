# data/interactive_stock_fetcher.py (V3 - Full UTC Unification)

"""
Interactive Brokers (IB) TWS API stock/index market data fetcher.
- Backtest Ready: Supports custom end_datetime
- Cache aware of end_datetime to avoid data pollution
- Reads configuration from YAML file
- FIXED (V2): Filename timestamp uses ISO 8601 UTC.
- FIXED (V3): DATA CONTENT (DataFrame Index) is now strictly converted to UTC. 
              Uses IB API formatDate=2 (Unix Timestamp) to avoid timezone ambiguity.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import Dict, List, Optional, Tuple, Union
from datetime import datetime, timezone

import pandas as pd
import yaml
from ibapi.client import EClient
from ibapi.common import BarData
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper


# ─────────────────────────── 辅助函数 ─────────────────────────── #

def ensure_utc(dt: Union[datetime, pd.Timestamp]) -> datetime:
    """
    统一时间转换工具：保证输出为带时区的 UTC datetime。
    """
    if isinstance(dt, pd.Timestamp):
        dt = dt.to_pydatetime()
    
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    
    return dt.astimezone(timezone.utc)


def format_iso_utc(dt: Union[datetime, pd.Timestamp]) -> str:
    """生成标准文件名后缀: YYYYMMDDTHHMMSSZ"""
    return ensure_utc(dt).strftime("%Y%m%dT%H%M%SZ")


# ─────────────────────────── 配置区 ─────────────────────────── #

@dataclass
class BarSchedule:
    """Single time resolution configuration"""
    label: str
    duration: str
    bar_size: str
    filename_duration: str
    filename_resolution: str
    use_rth: Optional[int] = None    # V16: per-schedule regular-hours switch; None → DEFAULT_USE_RTH by label


# V16 (2026-10-25): hourly and daily bars are fetched regular-session only for every market (mixed open/close buckets
# cannot be separated afterwards; daily H/L must not carry extended-hours prints). 5m / 1m keep the caller's choice
# (US extended hours stay available to the postcheck's 3-day touch check and to the debaters' bars calls).
DEFAULT_USE_RTH = {"2m_1h": 1, "5y_1d": 1}


@dataclass
class MarketDataConfig:
    """Market data configuration"""
    cache_dir: str
    schedules: List[BarSchedule]

    @classmethod
    def from_yaml(cls, yaml_path: str = "config/data_sources.yaml") -> "MarketDataConfig":
        path = Path(yaml_path)
        if not path.exists():
            path = Path(__file__).resolve().parents[2] / "config/data_sources.yaml"

        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        market_config = config["data_sources"]["market_data"]

        schedules = [
            BarSchedule(
                label=s["label"],
                duration=s["duration"],
                bar_size=s["bar_size"],
                filename_duration=s["filename_duration"],
                filename_resolution=s["filename_resolution"],
                use_rth=s.get("use_rth"),
            )
            for s in market_config["schedules"]
        ]

        return cls(
            cache_dir=market_config["cache_dir"],
            schedules=schedules
        )


@dataclass
class SymbolContract:
    """
    Encapsulates information needed to build IB Contract.

    symbol      = canonical id (英文简称, 落盘文件名/技术指标/PG/debate/prompt 全用它)。
    ibkr_ticker = 交易所询价代码 (如东证 7011 / KRX 005930)。**只**在 to_ib_contract()
                  造合约时用; 缺省 (美股/欧股不填) 时回退到 symbol, 行为不变。
    这层分离让"数字代码只用于请求数据", 而标的身份对外一律是英文, 避免 AI 把数字当价格。
    """
    symbol: str
    sec_type: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    primary_exchange: Optional[str] = None
    ibkr_ticker: Optional[str] = None

    def get_fs_symbol(self) -> str:
        return self.symbol.replace(" ", ".").upper()

    def _ib_symbol(self) -> str:
        """造 IB 合约用的 ticker: 有 ibkr_ticker 用它, 否则退回 canonical symbol。"""
        return self.ibkr_ticker or self.symbol

    def to_ib_contract(self) -> Contract:
        contract = Contract()
        ib_sym = self._ib_symbol()
        sym_upper = ib_sym.upper()

        if sym_upper == "BRK.B" or sym_upper == "BRK B":
            contract.symbol = "BRK B"
            contract.secType = self.sec_type
            contract.currency = self.currency
            contract.exchange = self.exchange
            contract.primaryExchange = "NYSE"
            print(f"[SYMBOL_CONVERT] {self.symbol} -> IB API: 'BRK B' (Force NYSE)")
            return contract

        contract.symbol = ib_sym
        contract.secType = self.sec_type
        contract.exchange = self.exchange
        contract.currency = self.currency

        if self.primary_exchange:
            contract.primaryExchange = self.primary_exchange

        return contract


# ──────────────────────── 缓存管理器 ──────────────────────── #

class MarketDataCacheManager:
    """Manages market data cache file I/O"""

    def __init__(self, config: MarketDataConfig):
        self.config = config
        self.base_dir = Path(config.cache_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_path(self, fs_symbol: str) -> Path:
        return self.base_dir / fs_symbol

    def _normalize_ib_time_str(self, end_datetime: str) -> str:
        if not end_datetime:
            return format_iso_utc(datetime.now(timezone.utc))
        try:
            dt = datetime.strptime(end_datetime, "%Y%m%d %H:%M:%S")
            # 假设用户输入的 IB 时间是 UTC (或将其当作 UTC 处理以保证文件名一致性)
            # 如果输入的是美东时间，这里可能产生偏差，但文件名主要是为了唯一标识。
            # 更严谨的做法是将 end_datetime 解析为 UTC，但 IB API 本身接受的是 Exchange Time 或 UTC。
            # 这里简单起见，将其转为 UTC 格式字符串。
            return format_iso_utc(dt)
        except ValueError:
            try:
                dt = datetime.strptime(end_datetime, "%Y%m%d")
                return format_iso_utc(dt)
            except ValueError:
                return end_datetime.replace(" ", "_").replace(":", "") + "Z"

    def _get_cache_filename(self, fs_symbol: str, schedule: BarSchedule, time_tag: str) -> str:
        return f"{fs_symbol}_{schedule.filename_duration}_{schedule.filename_resolution}_{time_tag}.csv"

    def cache_exists(self, fs_symbol: str, schedule: BarSchedule, end_datetime: str) -> bool:
        if not end_datetime:
            return False
        return False 

    def load_cache(self, fs_symbol: str, schedule: BarSchedule, end_datetime: str) -> Optional[pd.DataFrame]:
        return None

    def save_cache(self, fs_symbol: str, schedule: BarSchedule, df: pd.DataFrame, request_end_datetime: str) -> Path:
        """保存数据"""
        cache_dir = self._get_cache_path(fs_symbol)
        cache_dir.mkdir(parents=True, exist_ok=True)

        time_tag = ""
        
        if not df.empty:
            try:
                # 获取数据最后一行的时间索引
                # 此时 df.index 应该是已经转换好的 UTC DatetimeIndex
                last_timestamp = df.index[-1]
                time_tag = format_iso_utc(last_timestamp)
            except Exception as e:
                print(f"[CACHE][WARN] Could not extract timestamp from data: {e}")
        
        if not time_tag:
            time_tag = self._normalize_ib_time_str(request_end_datetime)
            
        filename = self._get_cache_filename(fs_symbol, schedule, time_tag)
        filepath = cache_dir / filename

        df.to_csv(filepath)
        print(f"[CACHE][INFO] Saved: {filepath.name}")
        return filepath


# ──────────────────────── IB 客户端封装 ──────────────────────── #

class InteractiveConnection(EWrapper, EClient):
    def __init__(self, api_version: int = 163):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self.api_version = api_version
        self._thread: Optional[Thread] = None

    def connect_and_start(self, host: str, port: int, client_id: int, warmup: float = 2.0) -> bool:
        self.connect(host, port, clientId=client_id)
        self._thread = Thread(target=self.run, daemon=True)
        self._thread.start()
        time.sleep(warmup)
        return self.isConnected()

    def disconnect_and_stop(self) -> None:
        if self.isConnected():
            self.disconnect()
        if self._thread and self._thread.is_alive():
            time.sleep(1.0)
        self._thread = None


class MarketDataFetcher(InteractiveConnection):
    def __init__(self, config: Optional[MarketDataConfig] = None):
        super().__init__()
        self._req_id_counter: int = 1
        self._data_store: Dict[int, List[BarData]] = {}
        self._completion_flags: Dict[int, bool] = {}

        self.config = config or MarketDataConfig.from_yaml()
        self.cache_manager = MarketDataCacheManager(self.config)

    def historicalData(self, reqId: int, bar: BarData) -> None:
        self._data_store.setdefault(reqId, []).append(bar)

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
        self._completion_flags[reqId] = True

    def error(self, reqId: int, errorCode: int, errorString: str) -> None:
        if errorCode not in [2104, 2106, 2158]:
            prefix = "Connection" if reqId == -1 else f"Request {reqId}"
            print(f"[IB][ERROR][{prefix}] code={errorCode}, msg={errorString}")

    def _next_req_id(self) -> int:
        req_id = self._req_id_counter
        self._req_id_counter += 1
        return req_id

    def _submit_request(
        self,
        req_id: int,
        contract: Contract,
        duration: str,
        bar_size: str,
        end_datetime: str = "",
        what_to_show: str = "TRADES",
        use_rth: int = 0,
        format_date: int = 2,  # 🔥 关键修改：默认使用 2 (Unix Timestamp in seconds)
        keep_up_to_date: bool = False,
    ) -> None:
        self.reqHistoricalData(
            reqId=req_id,
            contract=contract,
            endDateTime=end_datetime,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=format_date, # 请求 Unix 时间戳
            keepUpToDate=keep_up_to_date,
            chartOptions=[],
        )

    def _wait_for_completion(self, req_id: int, timeout: float) -> bool:
        start = time.time()
        while not self._completion_flags.get(req_id, False):
            if time.time() - start > timeout:
                print(f"[IB][WARN] Request {req_id} timed out after {timeout} seconds.")
                return False
            time.sleep(0.5)
        return True

    @staticmethod
    def _bars_to_dataframe(bars: List[BarData]) -> pd.DataFrame:
        rows = []
        for bar in bars:
            rows.append(
                {
                    "date": bar.date, # 此时 bar.date 可能是字符串 "1732655760" (Unix) 或 "20241126" (Day)
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "barCount": bar.barCount,
                    "wap": bar.average,
                }
            )
        df = pd.DataFrame(rows)
        
        # 🔥 核心修正：严格的 UTC 时间解析逻辑
        if not df.empty and "date" in df.columns:
            try:
                # 检查第一行是否是纯数字（Unix Timestamp 字符串）
                # 注意：日线数据(1 day)即使 formatDate=2 也可能返回 "YYYYMMDD"，需要兼容
                first_date = str(df["date"].iloc[0])
                
                if first_date.isdigit() and len(first_date) > 8:
                    # Case A: Unix Timestamp (Seconds) -> 转换为 UTC datetime
                    # pandas 3.x 要求先转 int，字符串直接传 unit='s' 会失败
                    df["date"] = pd.to_datetime(df["date"].astype(int), unit='s', utc=True)
                else:
                    # Case B: YYYYMMDD (String) -> 转换为 datetime 后本地化为 UTC
                    # 这样 "20241126" 会变成 "2024-11-26 00:00:00+00:00"
                    df["date"] = pd.to_datetime(df["date"])
                    if df["date"].dt.tz is None:
                        df["date"] = df["date"].dt.tz_localize(timezone.utc)
                        
            except Exception as e:
                print(f"[IB][WARN] Date parsing failed, index might be incorrect: {e}")
                # Fallback
                df["date"] = pd.to_datetime(df["date"])

        df = df.set_index("date").sort_index()
        return df

    # ── 对外方法 ──────────────────────────────────────────── #

    def fetch_bars(
        self,
        symbol_contract: SymbolContract,
        schedule: BarSchedule,
        *,
        end_datetime: str = "",
        what_to_show: str = "TRADES",
        use_rth: int = 1, 
        timeout: float = 60.0,
        spacing: float = 1.0,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        
        fs_symbol = symbol_contract.get_fs_symbol()
        req_id = self._next_req_id()
        contract = symbol_contract.to_ib_contract()

        # 非美股一律只取常规时段 (2026-09-26, NTDOY PTS 案): EU/JP/KR 没订盘外数据、
        # 辩论也都在常规时段跑; use_rth=0 时盈透会把 PTS 夜盘薄量成交归到下一交易日,
        # 日线凭空多出一个日期, 复盘窗口被误判。美股保留调用方的选择(盘前盘后是真信息)。
        # 2026-10-02 核实: 对东证个股(通用路由)这个开关不起作用, 真正的过滤在取回之后(见下面的 session_filter)。
        if symbol_contract.currency.upper() != "USD":
            use_rth = 1

        print(
            f"[MARKET_DATA][INFO] Requesting {fs_symbol} "
            f"(End: {end_datetime or 'NOW'}, Dur: {schedule.duration}, Bar: {schedule.bar_size})"
        )

        self._data_store.pop(req_id, None)
        self._completion_flags.pop(req_id, None)

        self._submit_request(
            req_id=req_id,
            contract=contract,
            duration=schedule.duration,
            bar_size=schedule.bar_size,
            end_datetime=end_datetime,
            what_to_show=what_to_show,
            use_rth=use_rth,
            format_date=2, # Explicitly request Unix Timestamps for UTC consistency
        )

        time.sleep(spacing)

        if not self._wait_for_completion(req_id, timeout=timeout):
            raise TimeoutError(f"Historical data request {req_id} timed out.")

        bars = self._data_store.get(req_id, [])
        if not bars:
            print(f"[MARKET_DATA][WARN] No data returned for {fs_symbol} (ReqID: {req_id})")
            return pd.DataFrame()

        df = self._bars_to_dataframe(bars)

        # 东证个股走通用路由会混入场外交易系统的时段, 且上面的 use_rth 对它不起作用(2026-10-02 核实):
        # 落盘前按交易所日历去掉场外时段的盘中 bar 与"交易日还没开盘"的假日线。别的市场原样通过。
        from datalayer.fetchers.session_filter import drop_off_exchange_bars
        df, flt = drop_off_exchange_bars(df, symbol_contract.symbol, schedule.bar_size)
        if flt.get("dropped"):
            print(f"[MARKET_DATA][INFO] {fs_symbol} {schedule.bar_size}: 去掉 {flt['dropped']} 根场外时段或未开盘交易日的 bar "
                  f"({flt['exchange']})")
        if df.empty:
            print(f"[MARKET_DATA][WARN] {fs_symbol} {schedule.bar_size}: 过滤后没有常规时段的 bar")
            return df

        # 保存 CSV (内容已经是 UTC)
        self.cache_manager.save_cache(fs_symbol, schedule, df, end_datetime)
        
        return df

    def fetch_multi_resolutions(
        self,
        symbol_contract: SymbolContract,
        schedules: Optional[List[BarSchedule]] = None,
        *,
        end_datetime: str = "",
        what_to_show: str = "TRADES",
        use_rth: int = 0,
        timeout: float = 60.0,
        spacing: float = 1.0,
        force_refresh: bool = False,
    ) -> Dict[str, pd.DataFrame]:
        
        if schedules is None:
            schedules = self.config.schedules

        # US 标的走 realtime(已订阅);非 USD(EU/JP 等无订阅)切 delayed-15min
        md_type = 1 if symbol_contract.currency.upper() == "USD" else 3
        try:
            self.reqMarketDataType(md_type)
        except Exception as exc:
            print(f"[MARKET_DATA][WARN] reqMarketDataType({md_type}) failed: {exc}")

        results: Dict[str, pd.DataFrame] = {}
        for schedule in schedules:
            try:
                df = self.fetch_bars(
                    symbol_contract=symbol_contract,
                    schedule=schedule,
                    end_datetime=end_datetime, 
                    what_to_show=what_to_show,
                    use_rth=(schedule.use_rth if schedule.use_rth is not None else DEFAULT_USE_RTH.get(schedule.label, use_rth)),
                    timeout=timeout,
                    spacing=spacing,
                    force_refresh=force_refresh,
                )
                if not df.empty:
                    results[schedule.label] = df
            except Exception as exc:
                print(
                    f"[MARKET_DATA][WARN] Failed to fetch {schedule.label} for {symbol_contract.symbol}: {exc}"
                )
        # V16: record the feed mode actually used so the snapshot can report session_context.feed_mode honestly
        try:
            import json as _json
            from datetime import datetime as _dt, timezone as _tz
            meta_path = self.cache_manager._get_cache_path(symbol_contract.get_fs_symbol()) / "_feed_meta.json"   # 11-06 fix: the method lives on the cache manager; the sidecar had never been written since V16
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(_json.dumps({"feed_mode": "realtime" if md_type == 1 else "delayed", "md_type": md_type,
                                              "feed_delay_minutes": 0 if md_type == 1 else 15,
                                              "fetched_at": _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}), encoding="utf-8")
        except Exception as exc:
            print(f"[MARKET_DATA][WARN] feed meta not written: {exc}")
        return results


if __name__ == "__main__":
    print("--- [Manual Runner] IBKR Fetcher (Full UTC Mode) ---")
    # fetcher = MarketDataFetcher()
    # fetcher.connect_and_start("127.0.0.1", 7496, 999)
    # contract = SymbolContract(symbol="SPY")
    # df = fetcher.fetch_bars(contract, fetcher.config.schedules[0])
    # print(df.head())
    # print(f"Index Timezone: {df.index.tz}")