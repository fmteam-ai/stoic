# PR: v1.60.57a — #55 live-feature engines (flag OFF, 1-week shadow compare) + #56 cross-account loss cooldown

Base: GitHub `main` @ `347f421` (v1.60.55) — this branch also carries the already-approved **v1.60.56** follow-ups
(#49–#54, no flags). Small review-first PR for the Sunday deploy; the gold_trend_pulse engine (items 1–14) follows as
v1.60.57b on top of it. Protected paths untouched. Not tagged. Notes: `docs/RELEASE_NOTES_v1.60.57a.md`.

| item | what | default behaviour |
|---|---|---|
| **#55** | `compute_intraday_features(bars, live_price, m5_bars)`: last_price / vwap_dist / Donchian / range_pos on the LIVE broker price (EMAs, ATR on closed bars); fast 1 h EMA20 slope; hf_scalp / hf_scalp_fast momentum-continuation override (FLAT but ≥ 0.5×ATR15 above VWAP, 3 h momentum, M5 closes rising, no LH_LL); breakout_m15 requires the live price ≥ 0.2×ATR15 beyond the broken level — a BREAK_DOWN whose live price is back inside is never sold; `stale_feature_entry` losses excluded from the anti-tilt streak; both arms recorded as `engine_shadow_compare` on every decision | `engine_live_features_enabled=false` → engines trade on closed-bar features exactly as before; shadow compare only |
| **#56** | `loss_cooldown_block` scans every account of the user: a bot close < −0.1R on ANY account pauses that symbol+direction on ALL accounts for the evaluating account's `loss_cooldown_minutes` (30); scratch/BE closes of other accounts don't trigger; pulse "Cross-account loss cooldown …", counter `cross_account_loss_cooldown_block` (16 Oct RoboForex → OnEquity 43 s re-entry reproduced in test) | always on — blocks only |

Files: `backend/bars_util.py` (new, shared helpers), `intraday_features.py`, `strategy_engines.py`, `ai_signals.py`,
`execution.py`, `streak_losses.py`, `models.py`, `bot_runner.py`, `loss_cooldown.py`, `scripts/engine_live_features_replay.py`,
`frontend/src/pages/BotConfig.jsx` (one toggle), tests (11), docs. Guards untouched (authority/CLOSE_ONLY, panic, Friday
flat, EOD, anti-tilt, daily loss, macro freeze, direction cap, pip-value, close_watchdog, broker-price management).

**Validation**: unit lane green on this branch (see PR checks); `scripts/engine_live_features_replay.py` is read-only and
needs an MT5 M1 export for the 16 Oct 13:00–14:30 window and the 30-day old-vs-new table — to be run on the VPS and
pasted here before the flag is switched ON (synthetic smoke only in this sandbox: the new arm signals MORE often on
noise, which is exactly why it ships OFF with the one-week shadow compare).
