# v1.60.53 — Data sources (advisory / shadow) + broker-price trade management

Scope: A) Finnhub calendar + news · B) geopolitical / central-bank advisory risk score · C) GVZ + VIX regimes ·
D) live tier management on the broker price · plus #37 (var_cap vs breakeven) and #38 (Scalp page / scalp
direction cap) from v1.60.52. Protected paths untouched (EX5, `policies/*`, `release/ea_release.json`, the EA
entry of `docs/RELEASE_HASHES.json`).

**Shadow rule.** Every new outside feed is ADVISORY ONLY: it logs, scores and displays, and is stamped on decisions
(`entry_context.risk_regime`, `gvz_regime`, `vix_regime`) so it can be evaluated — it does NOT block, open, close or
size any trade. No runtime gate reads `risk_off_score` or `geo_risk_gate_enabled` (guard test
`test_risk_regime_is_stamped_shadow_only_and_flag_has_no_gate`). Review window: 2–3 weeks, then George decides.

## New environment variables
| Variable | Default | Effect |
|---|---|---|
| `FINNHUB_API_KEY` | unset | Enables the Finnhub provider (calendar + forex/general news). Unset → provider disabled, Bot Health shows "Finnhub: not configured" (info). Never logged or returned. |
| `FINNHUB_API_KEY_FILE` | unset | Path to a file holding the key (secrets-file convention); env value wins. |
| `BROKER_PRICE_FRESH_MS` | `15000` | A broker price older than this is stale → the management cycle for that trade is skipped (no web fallback). |
| `BROKER_PRICE_AMBER_MS` | `60000` | Bot Health amber when any open live trade has no fresher broker price. |

No new FRED variable: `GVZCLS` / `VIXCLS` use the existing FRED key, transport and `fred_cache`.

## New Bot Config flags
| Flag | Default | Effect |
|---|---|---|
| `geo_risk_gate_enabled` | `false` | PLUMBING ONLY (B4). Stored and shown ("ON (NO EFFECT YET)") — no blocking behaviour exists behind it. |
| `require_h1_alignment` (v1.60.52) | `false` | hf_scalp / hf_scalp_fast entries skipped when the dominant H1 bias opposes the trade. |

## New collections / documents
- `risk_regime_advisory` — one doc per run: `geo_risk`, `regions`, `cb_event_active`, `gvz_regime` / `vix_regime`,
  `risk_off_score` (0–100), `components`, `top_drivers`, `bucket` (low < 35 ≤ med < 65 ≤ high), `shadow: true`.
- `geo_risk_history` — GDELT volume/tone per region, TTL 30 days (the z-score baseline).
- `central_bank_releases` — Fed / ECB / BoE / BoJ / SNB / RBA / BoC press-release titles + links (no full text).

## Behaviour changes (not shadow)
- **D — broker price for live trades.** `trade_manager._manage_one_trade` prices LIVE (EA-managed) trades from the
  EA's position report (`trade.live_price` + `live_price_at`; `live_bid` / `live_ask` when the EA sends them —
  BUY managed on bid, SELL on ask), with the scalp tick stream of the same account+symbol as the only fallback.
  Stale (> 15 s) or missing → the cycle is skipped with the pulse "broker price stale — management skipped"
  (one per trade per 5 min). `market.get_quote()` remains for UI, agents, paper trades and analysis only.
- **A3 — calendar source order.** `economic_calendar.SOURCE_ORDER = (finnhub, forexfactory, financecalendar)`;
  Finnhub rows win the (currency, minute) merge; each event carries `source`, Finnhub rows also `actual` and
  `surprise`. Cache TTL follows the Finnhub cadence (5 min, 30 s within ±10 min of a high-impact print) when the
  key is set; the legacy 1 h otherwise. HTTP 401/403 on an endpoint → logged once, endpoint marked unavailable,
  other sources kept. 429 → exponential backoff (60 s → 15 min).
- **A2 — country mapping.** Explicit ISO map (US→USD, EU/DE/FR/IT/ES/NL→EUR, GB/UK→GBP, JP→JPY, AU→AUD, CA→CAD,
  CH→CHF, NZ→NZD, CN→CNY); anything else → `OTHER` (never USD / ALL).
- **A5 — news.** Finnhub forex + general news polled every 2 min, deduped by URL/title hash against NewsAPI/RSS,
  currency/XAU tagged, relevance pre-filter before the LLM scorer (`news_understanding.fetch_market_news`).

## UI
- Dashboard Finance Calendar: `CALENDAR SOURCE · <source> · <status> · REFRESHED hh:mm` line and a
  "RELEASED · LAST 6H · ACTUAL / FORECAST / SURPRISE" panel.
- Bot Health (admin): "Risk regime (advisory)" card — score, top 3 drivers, context, last update,
  "SHADOW — NOT AFFECTING TRADES". Issues: `finnhub_not_configured` (info), `gvz_stale` / `vix_stale` (amber,
  > 4 trading days), `broker_price_stale` (amber, per account); `broker_price` per-account ages in the payload.
- Agents › Macro: GVZ (Gold Vol) and VIX tiles next to the 10Y real yield with calm/normal/elevated/stressed + P20.
- Loss Lab › Strategy Report: "RISK-OFF SCORE AT ENTRY" win rate / expectancy R by bucket (B6).
- Bot Config: GEO / RISK-OFF GATE · SHADOW row (flag plumbing).

## Operational notes
- GDELT allows one request per 5 s and throttles shared egress IPs; the poller spaces requests, stops after two
  429s and records `gdelt_error` on the advisory — the geo component is then simply `missing` until the next cycle.
- Central-bank RSS feeds verified live (55 releases ingested on first poll).
- Tests: `test_v1_60_53_data_sources_broker_price.py` (8), `test_v1_60_52_var_cap_scalp.py` (4),
  `test_v1_60_52_followups.py` (11). Legacy trade-manager tests moved from the `get_quote` seam to
  `trade_manager._broker_snapshot` (`tests/unit/broker_price_stub.py`).
