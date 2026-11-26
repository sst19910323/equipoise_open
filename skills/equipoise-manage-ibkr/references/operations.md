# IBKR operations reference

## Canonical files

- Service source: `/opt/equipoise/systemd/ibgateway.service`
- Installed unit: `/etc/systemd/system/ibgateway.service`
- IBC config: `/root/ibc/config.ini`
- Start/restart: `/opt/equipoise/ibkr_restart.sh`
- Stop for mobile login: `/opt/equipoise/ibkr_stop.sh`
- Three-layer probe: `/opt/equipoise/ibkr_probe.py`
- IBC logs: `/root/ibc/logs/`

## Ports

- `4001`: live IB Gateway API
- `4002`: paper IB Gateway API
- `7496`: live TWS API
- `7497`: paper TWS API

## Market-data signals

- Market data type `1`: live
- Market data type `2`: frozen
- Market data type `3`: delayed
- Market data type `4`: delayed-frozen
- Error `354`: requested market data is not subscribed
- Error `10089`: requested data requires an additional subscription
- Error `10167`: requested market data is unavailable; delayed data may be available

Do not infer real-time entitlement merely from a successful historical daily-bar request. Confirm with a streaming quote during market hours and inspect the returned market-data type.

## Subscriptions held (account entitlements, snapshot 2026-07-17)

Use this to tell a genuine entitlement error (354 / 10089) from a delayed fallback in a
region we simply do not subscribe. "L1" = top-of-book, "L2" = depth, "NP" = non-professional.

### North America
- Canada (TSE Group + Montréal derivatives) bundle — NP, L2 — free
- ICE US Futures – Digital Asset Futures — L2 — free
- ICE US Futures – Gold/Silver — L2 — free
- IEX Depth of Book — NP, L2 — free
- PAXOS IBLLC-US (non-US) — free
- US & European Bond Quotes — L2 — free
- **US Equity and Options Add-On Streaming Bundle — NP — USD 4.50 / mo** (paid)
- US Mutual Funds — NP, L1 — free
- **US Real-Time Non-Consolidated Streaming Quotes** — free
- **US Securities Snapshot and Futures Value Bundle — NP, L1 — USD 10.00 / mo** (paid; waived if monthly commissions ≥ USD 30)

### Europe
- Alternative European Equities — L1 — free
- Aquis Exchange — NP, L1 — free
- Eurex Core — NP, L1 — free
- Eurex Retail Europe — NP, L1 — free
- European (BATS/Chi-X) Equities — NP, L2 — free
- European Mutual Funds — free
- **German ETFs — NP, L1** — free

### Middle East
- Dubai Financial Market — NP, L1 — free

### Asia-Pacific
- **Korea Equities Bundle — NP, L1** — free

### Global
- IDEALPRO FX (IDEALPRO) — free
- US & European Bond Quotes — L1 — free

### What this means for Equipoise data pulls
- **US equities/ETFs**: no full consolidated (SIP) real-time streaming tape. What we have is
  *non-consolidated* real-time streaming (single-venue, e.g. IEX) + a *snapshot* L1 bundle.
  Consistent with the observed ~15 min lag on historical minute bars — hence
  `market_data_freshness.US = 20` in `config/data_sources.yaml`. Do not assume type-`1` full
  entitlement; verify the returned market-data type per symbol.
- **Europe**: German ETFs L1 + Eurex + BATS/Chi-X (L2) + Aquis + Alternative European are
  entitled → DAX/German-listed names (GENERAL_EU, RHM/BESI/HAG on XETRA/Euronext) can get L1.
- **Korea**: Korea Equities Bundle (L1) IS entitled → the KR region symbols
  (SSNLF/HXSCL/HANMISEMI/HANWHAAERO/DOOSANENER, benchmark KODEX200 ETF since 2026-09-28 —
  the K200 index HMDS feed died 2026-09-18) should get real-time L1 on KRX
  once contracts resolve. Entitlement is not the blocker for bringing KR `runnable: true`.
- **Japan**: no native TSE real-time subscription held → native TSE quotes fall back to delayed.
  The JP canonical symbols we run are named by their ADR tickers (NTDOY, SFTBY, …) but the contract
  requested is the TSE listing (`ibkr_ticker` numeric code, `primary_exchange: TSEJ`, JPY) — delayed
  quotes in yen, not USD ADR prices.
- **FX**: IDEALPRO entitled (real-time).
- Not held: any China A / HK / broad Asia-ex-Korea real-time equity feed.

## Incident signature fixed in July 2026

The old unit used `Restart=always` every 15 seconds while an IBC child escaped service ownership. The unit accumulated more than 5,600 retries reporting `process is already running`, although the Java Gateway remained logged in. Prevent recurrence by using one systemd-owned cgroup, no unattended restart, and explicit human-gated 2FA.
