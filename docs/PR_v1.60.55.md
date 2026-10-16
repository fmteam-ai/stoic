# PR: v1.60.55 — review fixes for PR #102–#105 (items #44–#48)

Base: `main` @ `8de8bfd`. No tag. Protected EA assets, policies, release records and hashes untouched.
Full notes: `docs/RELEASE_NOTES_v1.60.55.md`.

## What changes (trading-affecting, all default-safe)
| # | Change | Default / flag |
|---|--------|----------------|
| 44 | `close_watchdog`: mandatory closes (panic, friday_flat, eod_flatten, protection, manual, NL) are **never expired**; re-dispatched as a fresh fenced FULL_CLOSE with backoff 60 s → 15 min until the terminal confirms or the position is gone. Discretionary closes (var_cap, deleverage, time-stop, TP, scalp exits) expire after 300 s **only while the EA heartbeat is ≤ 90 s old**. 60 s unacked alert unchanged. | `CLOSE_REQUEST_TTL_SEC=300`, `CLOSE_WATCHDOG_EA_ONLINE_SEC=90`, `CLOSE_REDISPATCH_BASE_SEC=60`, `CLOSE_REDISPATCH_MAX_SEC=900` |
| 45 | Sector-cap / combined-corr deleverage is **advisory only** (alert `sector_cap_advisory` + pulse, no close). Same zero-risk / stop-bounded exemptions as var_cap. The v1.60.54 notional model (100,000 units/lot forex, ×100 gold aliases) made the 600 % / 300 % / 500 % caps reachable for the first time — this flag keeps that from becoming a new auto-close. | `SECTOR_CAP_DELEVERAGE_ENABLED=false` |
| 46 | One shared break-even default `1.0R` (runtime + UI), `breakeven_enabled` honoured, BE stop placed at entry ± spread+commission (never a small loss), dry-run migration report. No stored config is rewritten. | `breakeven_enabled=true`, `breakeven_trigger_r` missing → 1.0R |
| 47 | Scalp direction-cap reservation uses `base_symbol()`; `_skip_pulse_at` pruned; legacy `live_at` is only accepted as a fresh broker price on EA ≥ 1.66. | — |
| 48 | VaR deleverage issues a real fenced FULL_CLOSE (confirmed by test). Friday flat / EOD flatten / panic **never skip** a `close_requested` trade: an unacked request is taken over by their own mandatory FULL_CLOSE, re-issued with backoff until the position is confirmed closed. | — |

## Finnhub disclosure (when `FINNHUB_API_KEY` is configured)
* Finnhub is the **primary calendar source** for `macro_freeze_check` — a real trading gate (fallback to the previous sources when unavailable).
* Finnhub headlines feed `news_understanding` and can influence **veto / confidence / size trim**.
* Not configured → disabled, previous behaviour.

## Break-even migration dry-run (`backend/scripts/report_breakeven_migration.py`)
```
DRY RUN — 5 bot configs, 0 effective break-even values change on deploy:
  same   cfg=6a6667cd22a7db8277ca9385 acct=6a6667c922a7db8277ca9374   enabled=None  stored=1.0   be_enabled=True  before=1R → after=1R
  same   cfg=6a90749929947512f0f704c0 acct=6a435003545b76de9539d197   enabled=None  stored=0.4   be_enabled=True  before=0.4R → after=0.4R
  same   cfg=6ac42e4e48f2ac06be456260 acct=6ab16a218826422d57dc7d33   enabled=None  stored=1.0   be_enabled=True  before=1R → after=1R
  same   cfg=6acc93310ac5f3d3ded727b5 acct=6acc93310ac5f3d3ded727b4   enabled=None  stored=1.0   be_enabled=True  before=1R → after=1R
  same   cfg=6acd6247780923e783979a2c acct=6acd6247780923e783979a2b   enabled=None  stored=1.0   be_enabled=True  before=1R → after=1R
```
Re-run on production before deploy (`cd backend && python scripts/report_breakeven_migration.py`); `--apply` is not implemented — the script only reports.

## Tests
* New: `tests/unit/test_v1_60_55_review_items.py` (13), `tests/integration/test_v1_60_54_close_watchdog.py` extended (mandatory re-dispatch, Friday/EOD takeover, acked request left alone).
* Updated: eod_flatten, r25 close-writers, iter52 friday_flat, v1.60.53/54 broker-price + BE assertions.
* Release artifacts regenerated (`docs/TEST_MANIFEST.md`, `docs/RELEASE_SUMMARY.md`, `release/rc_lock.json`).

## Deploy window
Deploy only while the market is closed (weekend) — **not before Friday 20:00 UTC Friday flat**. After deploy, watch `close_command_unacked` alerts and the watchdog `redispatched` counter on the first Friday flat / EOD cycle.
