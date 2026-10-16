# STOIC v1.60.56 — follow-ups to the v1.60.55 review (#49–#54)

Protected paths untouched. Unit tests for every item (`tests/unit/test_v1_60_56_followups.py`, 14) + real-Mongo e2e
(`tests/integration/test_v1_60_54_close_watchdog.py`, +2). No new trading behaviour is switched ON; every change
narrows what the server sends or alerts.

## #49 — alert / Telegram spam from mandatory re-dispatch
* `close_command_unacked` is deduped **per trade + reason** (`close_unacked:{trade}:{reason}`): every re-dispatched
  ledger row re-uses the one open alert (occurrence count grows) instead of opening a new one. The bot pulse fires only
  when the alert is NEW. Escalation ladder: **critical at 15 min** and **again at 1 h** since the FIRST unconfirmed
  request on the trade (`close_redispatch.first_at`), each rung once (`trade.close_unacked_escalations`).
* The re-dispatch pulse fires once per trade (first re-dispatch), not per re-dispatch.
* friday_flat sends `notify_friday_flat` (Telegram) **once per trade** — on the first touch, never on a re-issue;
  eod_flatten's in-app notification likewise counts only newly issued closes.
* **EOD quiet window**: the EA drops FULL_CLOSE 23:40–00:05 broker time (`ApplyFullClose`). The watchdog and
  `mandatory_close` mirror that window from the account's learned `broker_utc_offset_sec` (and honour an explicit
  heartbeat flag `ea_quiet_window` if a future EA reports one): while quiet, **no re-send and no alert**
  (`sweep()["quiet"]`, ledger `quiet_paused_at`); re-dispatch resumes when the window ends. A FIRST issue is never
  deferred (the EA defers it itself; the server picks it up after the window).

## #50 — friday_flat vs eod_flatten no longer fight
* `mandatory_close`: ANY pending **mandatory** reason counts as "own" — the backoff holds across callers (eod_flatten
  does not re-issue friday_flat's 10 s old command every tick and vice-versa). When a re-issue is due, the **strongest
  reason is kept**: `panic` > protection (`pre_news_protect`, `emergency_*`, `late_fill_after_*`, `intentional_close`,
  `hard_drawdown`) > `friday_flat` > `eod_flatten` > `manual` / `nl_command`. friday_flat can no longer overwrite a
  pending panic (its `close_reason_pending` stamp follows the kept reason). A pending DISCRETIONARY request
  (var_cap …) is still taken over immediately.

## #51 — netting legs
* An acked volume-limited netting leg (`netting_close_acked=true`) is excluded from the watchdog re-dispatch, from
  `mandatory_close` (returns `in_flight`) and from the panic FULL_CLOSE fan-out: the OUT deal closes it; a late deal
  (> 120 s) can no longer trigger a second FULL_CLOSE that over-closes volume belonging to another trade.

## #52 — more mandatory reasons; ack grace before expiry
* `late_fill_after_expiry`, `late_fill_after_lock`, `late_fill_after_panic`, `late_fill_after_cancel`,
  `intentional_close`, `unprotected_position`, `hard_drawdown` / `auto_deleverage_hard_drawdown` are **mandatory**
  (never expired, re-dispatched). var_cap / sector_cap / combined_corr deleverage, time-stop, TP and scalp exits stay
  discretionary.
* A discretionary close is **not expired within `CLOSE_ACK_GRACE_SEC` (120 s) of a successful terminal ack** — the
  OUT deal gets time to land before management is released.

## #53 — real entry costs for the break-even buffer; SELL tests; optimizer guard
* New `entry_costs.py`. On the first OPEN fill report the trade stores `spread_at_entry_pips` from the EA's latest
  heartbeat spread for the symbol; the STOIC trade's IN deal (`/bridge/external-deal`, also the adopted-sibling path)
  stores `commission_entry`, `commission_per_lot` and `commission_pips` (round trip ≈ 2 × entry, in pips of the symbol
  via `pip_value_usd_per_lot`). `scalp_cost_pips` already prefers these over the fixed defaults (gold 2.5 / FX 1.5
  pips, commission 0) — so the BE stop now sits at entry ± the REAL spread + commission. Trades opened before this
  release keep the defaults (nothing is back-filled).
* SELL break-even covered by tests (stop moves to entry − costs, never loosens).
* `breakeven_enabled` is **no longer optimizer-tunable** (removed from `ai_optimizer.ALLOWED_FIELDS`; an LLM
  recommendation for it is dropped and `apply_recommendation` refuses non-whitelisted fields). A preset switch can
  never turn break-even off (`breakeven_enabled` joined `STRICTER_OR`). Only the user does, in Bot Config.

## #54 — sector-cap advisory pulse dedup
* `_advise_sector_caps` pulses only when `raise_alert` opened a NEW alert; a breach that persists across sweeps keeps
  one alert (occurrences) and one pulse.

## New env / config in v1.60.56
| name | default | effect |
|---|---|---|
| — | — | no new flags; `CLOSE_ACK_GRACE_SEC` (v1.60.55, 120 s) now also gates discretionary expiry |

## Trade fields added
`spread_at_entry_pips`, `commission_entry`, `commission_per_lot`, `commission_pips`, `close_redispatch.first_at`,
`close_unacked_escalations[]`; ledger `close_commands.quiet_paused_at`.
