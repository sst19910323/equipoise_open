---
name: equipoise-manage-ibkr
description: Safely operate and diagnose the Equipoise Interactive Brokers Gateway/IBC service. Use when the user asks to stop or start IBKR, switch to mobile login, restart after 2FA, check API port 4001, investigate repeated logins, delayed or unavailable market data, Gateway hangs, or compare IB Gateway with GUI TWS.
---

# Manage Equipoise IBKR

Operate IBKR as a human-gated production dependency. Never create a repeated 2FA/login loop.

## Safety rules

- Treat `ibgateway.service` as the only process owner. Never start `/opt/ibc/gatewaystart.sh` with `nohup`.
- Keep exactly one Gateway/IBC Java process tree.
- Use `/opt/equipoise/ibkr_stop.sh` when the user needs mobile or TWS login.
- Use `/opt/equipoise/ibkr_restart.sh` for one explicit start attempt. It waits for 2FA and runs the data probe.
- Do not enable or start the service merely because configuration was edited.
- Do not retry a failed 2FA automatically. Report the log and wait for the user.

## Inspect

Run read-only checks first:

```bash
systemctl status ibgateway.service --no-pager
pgrep -af 'ibcalpha|ibgateway|gatewaystart'
ss -ltnp | rg '4001|4002|7496|7497'
```

Interpret service state and actual processes together. An inactive unit with a live Java process is a process-ownership failure.

## Stop for mobile login

Run `bash /opt/equipoise/ibkr_stop.sh`, then confirm no matching process and no IB API listener remain. Do not restart until the user explicitly asks.

## Start or restart

Confirm the user is available to approve IBKR Mobile 2FA, then run:

```bash
cd /opt/equipoise
bash ibkr_restart.sh
```

Exit codes: `0` healthy, `1` login/port failure, `2` connected but real data failed, `3` stale process remained, `64` invalid probe symbol.

## Diagnose market data

Run the live data probe only while the Gateway is intentionally logged in. Check whether code requests market data type `1`, then distinguish subscription errors from delayed fallback and data-farm outages. Read [references/operations.md](references/operations.md) for local paths and error codes.

## Change service configuration

Treat `/opt/equipoise/systemd/ibgateway.service` as the canonical source. Install it to `/etc/systemd/system/ibgateway.service`, run `systemctl daemon-reload`, and verify with `systemd-analyze verify`. Leave the service stopped unless the user authorizes a login attempt.
