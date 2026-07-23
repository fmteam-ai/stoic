# STOIC — API Reference

Auto-generated from the FastAPI OpenAPI schema (300 operations). Regenerate with `python scripts/generate_api_docs.py`.

## Conventions
- All routes are prefixed with `/api` and served on port 8001.
- **Auth**: httpOnly `access_token` cookie (JWT) + CSRF double-submit header `X-CSRF-Token` on mutating requests. Enterprise `/v1/*` routes use `X-API-Key` instead.
- **Step-up MFA**: live activation, risk raises, panic release and API-key creation additionally require `X-Step-Up-Token` obtained from `POST /api/auth/step-up` (fresh TOTP, single-use, 5 min).
- **Bridge routes** (`/api/bridge/*`) authenticate the MT5 EA with a per-account `bridge_token`.
- `GET /api/metrics` (Prometheus) is gated by `X-Metrics-Token`.

## accounts (21)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/accounts` | List Accounts |
| `POST` | `/api/accounts` | Create Account |
| `GET` | `/api/accounts/broker-comparison` | Broker Comparison |
| `GET` | `/api/accounts/broker-presets` | Broker Presets |
| `GET` | `/api/accounts/certification` | Accounts Certification |
| `GET` | `/api/accounts/equity-curve` | Accounts Equity Curve |
| `GET` | `/api/accounts/limits` | Account Limits |
| `GET` | `/api/accounts/overview` | Accounts Overview |
| `PATCH` | `/api/accounts/{account_id}` | Update Account |
| `DELETE` | `/api/accounts/{account_id}` | Delete Account |
| `GET` | `/api/accounts/{account_id}/block-status` | Block Status |
| `POST` | `/api/accounts/{account_id}/certify` | Certify Account |
| `PATCH` | `/api/accounts/{account_id}/credentials` | Update Credentials |
| `POST` | `/api/accounts/{account_id}/credentials/reveal` | Reveal Credentials |
| `POST` | `/api/accounts/{account_id}/import-positions` | Import Positions |
| `POST` | `/api/accounts/{account_id}/request-sync` | Request Broker Sync |
| `POST` | `/api/accounts/{account_id}/rotate-token` | Rotate Token |
| `PUT` | `/api/accounts/{account_id}/symbol-suffix` | Set Symbol Suffix |
| `GET` | `/api/accounts/{account_id}/test-connection` | Test Connection |
| `POST` | `/api/accounts/{account_id}/test-trade` | Fire Test Trade |
| `POST` | `/api/accounts/{account_id}/unblock` | Unblock Account Route |

## admin (12)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/admin/affiliates` | Admin List Affiliates |
| `POST` | `/api/admin/affiliates/{aff_id}/suspend` | Admin Suspend Affiliate |
| `POST` | `/api/admin/affiliates/{aff_id}/terminate` | Admin Terminate Affiliate |
| `POST` | `/api/admin/affiliates/{aff_id}/unsuspend` | Admin Unsuspend Affiliate |
| `GET` | `/api/admin/audit-log` | Admin Audit Log |
| `GET` | `/api/admin/users` | Admin List Users |
| `GET` | `/api/admin/users/{user_id}` | Admin User Detail |
| `POST` | `/api/admin/users/{user_id}/restore` | Admin Restore User |
| `POST` | `/api/admin/users/{user_id}/suspend` | Admin Suspend User |
| `POST` | `/api/admin/users/{user_id}/terminate` | Admin Terminate User |
| `POST` | `/api/admin/users/{user_id}/unsuspend` | Admin Unsuspend User |
| `GET` | `/api/terms` | Public Terms |

## admin-migration (2)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/admin/export-state` | Export State |
| `POST` | `/api/admin/import-state` | Import State |

## affiliate (13)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/admin/affiliate/applications` | Admin List |
| `POST` | `/api/admin/affiliate/applications/{app_id}/approve` | Admin Approve |
| `POST` | `/api/admin/affiliate/applications/{app_id}/reject` | Admin Reject |
| `GET` | `/api/admin/affiliate/commissions` | Admin Commissions |
| `POST` | `/api/admin/affiliate/commissions/{cid}/mark-paid` | Admin Mark Paid |
| `GET` | `/api/admin/affiliate/payout-requests` | Admin List Payout Requests |
| `POST` | `/api/admin/affiliate/payout-requests/{rid}/process` | Admin Process Payout |
| `POST` | `/api/affiliate/apply` | Affiliate Apply |
| `GET` | `/api/affiliate/payout-requests` | List My Payout Requests |
| `POST` | `/api/affiliate/request-payout` | Request Payout |
| `GET` | `/api/affiliate/stats` | Affiliate Stats |
| `GET` | `/api/affiliate/status` | Affiliate Status |
| `GET` | `/api/r/{code}` | Referral Redirect |

## agents (4)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/agents/activity` | List Activity |
| `GET` | `/api/agents/latest` | Latest Per Symbol |
| `GET` | `/api/agents/macro` | Macro Snapshot |
| `GET` | `/api/agents/report-card` | Report Card |

## analytics (13)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/analytics/attribution` | Get Attribution |
| `GET` | `/api/analytics/auto-tune` | Get Auto Tune |
| `POST` | `/api/analytics/auto-tune/refresh` | Refresh Auto Tune |
| `GET` | `/api/analytics/by-account` | Get By Account |
| `GET` | `/api/analytics/learned-meta` | Get Learned Meta |
| `GET` | `/api/analytics/learned-meta/drift` | Get Drift Status |
| `POST` | `/api/analytics/learned-meta/drift/check-now` | Check Drift Now |
| `POST` | `/api/analytics/learned-meta/retrain` | Retrain Learned Meta |
| `GET` | `/api/analytics/research` | Research |
| `GET` | `/api/analytics/rr-watch` | Rr Watch |
| `GET` | `/api/analytics/sessions` | Get Sessions |
| `POST` | `/api/analytics/sessions/apply-action` | Apply Session Action |
| `POST` | `/api/analytics/sessions/suggest-action` | Suggest Session Action |

## architecture (1)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/architecture` | Get Architecture |

## auth (20)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/auth/2fa/disable` | Two Fa Disable |
| `POST` | `/api/auth/2fa/enroll` | Two Fa Enroll |
| `GET` | `/api/auth/2fa/status` | Two Fa Status |
| `POST` | `/api/auth/2fa/verify-enroll` | Two Fa Verify Enroll |
| `GET` | `/api/auth/audit` | My Audit Trail |
| `POST` | `/api/auth/change-password` | Change Password |
| `GET` | `/api/auth/csrf` | Csrf Bootstrap |
| `POST` | `/api/auth/forgot-password` | Forgot Password |
| `POST` | `/api/auth/login` | Login |
| `POST` | `/api/auth/logout` | Logout |
| `GET` | `/api/auth/me` | Me |
| `PUT` | `/api/auth/profile` | Update Profile |
| `POST` | `/api/auth/refresh` | Refresh Token |
| `POST` | `/api/auth/register` | Register |
| `POST` | `/api/auth/resend-activation` | Resend Activation |
| `POST` | `/api/auth/reset-password` | Reset Password |
| `GET` | `/api/auth/sessions` | List Sessions |
| `POST` | `/api/auth/sessions/revoke-all` | Revoke All |
| `POST` | `/api/auth/step-up` | Step Up Verify |
| `POST` | `/api/auth/verify-email` | Verify Email |

## auto-heal (4)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/auto-heal/log` | Get Log |
| `POST` | `/api/auto-heal/run-now` | Run Now |
| `GET` | `/api/auto-heal/settings` | Get Settings |
| `POST` | `/api/auto-heal/settings` | Set Settings |

## bot (22)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/bot/adaptive-status` | Adaptive Status |
| `GET` | `/api/bot/config` | Get Config |
| `PUT` | `/api/bot/config` | Update Config |
| `DELETE` | `/api/bot/config` | Reset Account Config |
| `GET` | `/api/bot/configs` | List Configs |
| `GET` | `/api/bot/cooldowns` | Get Cooldowns |
| `GET` | `/api/bot/doctor` | Bot Doctor |
| `GET` | `/api/bot/execution-health` | Execution Health |
| `GET` | `/api/bot/health-score` | Bot Health Score |
| `GET` | `/api/bot/mtf-confluence` | Mtf Confluence Report |
| `POST` | `/api/bot/my-presets` | Save User Preset |
| `DELETE` | `/api/bot/my-presets/{preset_id}` | Delete My Preset |
| `POST` | `/api/bot/preset/{key}` | Apply Strategy Preset |
| `GET` | `/api/bot/presets` | Get Strategy Presets |
| `GET` | `/api/bot/pulse` | Get Bot Pulse |
| `GET` | `/api/bot/quick-actions` | Quick Actions |
| `GET` | `/api/bot/risk-gauge` | Get Risk Gauge |
| `GET` | `/api/bot/safety-status` | Safety Status |
| `GET` | `/api/bot/sizing-preview` | Sizing Preview |
| `POST` | `/api/bot/start` | Start Bot |
| `GET` | `/api/bot/status` | Get Bot Status |
| `POST` | `/api/bot/stop` | Stop Bot |

## bridge (9)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/bridge/candles` | Receive Candles |
| `POST` | `/api/bridge/dom` | Receive Dom |
| `POST` | `/api/bridge/external-deal` | External Deal |
| `POST` | `/api/bridge/heartbeat` | Heartbeat |
| `POST` | `/api/bridge/modification-ack` | Modification Ack |
| `POST` | `/api/bridge/poll-trades` | Poll Trades |
| `POST` | `/api/bridge/report` | Report Trade |
| `POST` | `/api/bridge/sync-complete` | Sync Complete |
| `POST` | `/api/bridge/ticks` | Receive Ticks |

## bugs (4)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/bugs` | Create Bug |
| `GET` | `/api/bugs` | List Bugs |
| `GET` | `/api/bugs/{bug_id}` | Get Bug |
| `PATCH` | `/api/bugs/{bug_id}/status` | Update Status |

## calendar (3)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/calendar` | All Events |
| `GET` | `/api/calendar/freeze/{symbol}` | Freeze |
| `GET` | `/api/calendar/upcoming/{symbol}` | Upcoming |

## copilot (3)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/copilot/chat` | Copilot Chat Endpoint |
| `GET` | `/api/copilot/sessions` | Copilot Sessions |
| `GET` | `/api/copilot/sessions/{session_id}` | Copilot Session Detail |

## crypto (9)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/crypto/accounts` | List Crypto Accounts |
| `POST` | `/api/crypto/accounts` | Create Crypto Account |
| `DELETE` | `/api/crypto/accounts/{account_id}` | Delete Crypto Account |
| `GET` | `/api/crypto/accounts/{account_id}/balance` | Crypto Balance |
| `POST` | `/api/crypto/accounts/{account_id}/execute` | Crypto Execute |
| `GET` | `/api/crypto/accounts/{account_id}/ticker` | Crypto Ticker |
| `POST` | `/api/crypto/accounts/{account_id}/verify` | Crypto Verify |
| `GET` | `/api/crypto/exchanges` | Crypto Exchanges |
| `GET` | `/api/crypto/status` | Crypto Status |

## diagnostic (2)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/diagnostic/auto-fix` | Auto Fix |
| `GET` | `/api/diagnostic/run` | Run Diagnostic |

## enterprise-api-keys (3)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/api-keys` | List Api Keys |
| `POST` | `/api/api-keys` | Create Api Key |
| `POST` | `/api/api-keys/{key_id}/revoke` | Revoke Api Key |

## enterprise-public-api (4)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/accounts` | V1 Accounts |
| `GET` | `/api/v1/me` | V1 Me |
| `GET` | `/api/v1/portfolio` | V1 Portfolio |
| `GET` | `/api/v1/trades` | V1 Trades |

## entitlements (2)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/entitlements/me` | My Entitlements |
| `GET` | `/api/entitlements/tiers` | Public Tier Matrix |

## execution-intelligence (4)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/execution/preview` | Execution Preview |
| `GET` | `/api/execution/quality` | Execution Quality |
| `POST` | `/api/execution/schedule` | Create Schedule |
| `GET` | `/api/execution/schedules` | List Schedules |

## health (1)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/data-freshness` | Data Freshness |

## insights (2)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/insights/weekly-digest` | Weekly Digest |
| `POST` | `/api/insights/weekly-digest/email` | Email Weekly Digest |

## integrity (1)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/integrity/snapshot` | Integrity Snapshot |

## macro (3)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/macro/freeze/{symbol}` | Macro Freeze |
| `GET` | `/api/macro/gate` | Macro Gate Status |
| `GET` | `/api/macro/snapshot` | Macro Snapshot |

## market (5)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/market/history/{symbol}` | History |
| `GET` | `/api/market/quote/{symbol}` | Quote |
| `GET` | `/api/market/quotes` | Quotes |
| `GET` | `/api/market/risk-profiles` | Risk Profiles |
| `GET` | `/api/market/symbols` | List Supported Symbols |

## metrics (1)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/metrics` | Metrics |

## misc (7)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/` | Root |
| `GET` | `/api/bridge/download-ea` | Ea Script |
| `GET` | `/api/ea-script` | Ea Script |
| `GET` | `/api/health` | Health |
| `GET` | `/api/health/live` | Health Live |
| `GET` | `/api/health/ready` | Health Ready |
| `GET` | `/api/setup/installer.ps1` | Installer Script |

## ml (2)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/ml/ensemble` | Get Ensemble Status |
| `POST` | `/api/ml/train` | Retrain Ensemble |

## nl-commander (10)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/nl/command` | Nl Command |
| `POST` | `/api/nl/command/confirm` | Nl Command Confirm |
| `POST` | `/api/nl/strategy` | Nl Strategy |
| `POST` | `/api/nl/strategy/apply` | Nl Strategy Apply |
| `POST` | `/api/nl/strategy/backtest` | Nl Strategy Backtest |
| `POST` | `/api/nl/strategy/code` | Nl Strategy Code |
| `POST` | `/api/nl/strategy/optimize` | Nl Strategy Optimize |
| `GET` | `/api/nl/strategy/targets` | Nl Strategy Targets |
| `GET` | `/api/nl/triggers` | List Triggers |
| `DELETE` | `/api/nl/triggers/{trigger_id}` | Delete Trigger |

## notifications (5)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/notifications/prefs` | Get Telegram |
| `GET` | `/api/notifications/settings` | Get Telegram |
| `GET` | `/api/notifications/telegram` | Get Telegram |
| `PUT` | `/api/notifications/telegram` | Update Telegram |
| `POST` | `/api/notifications/telegram/test` | Test Telegram |

## optimizer (5)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/optimizer/analyze` | Run Analysis |
| `GET` | `/api/optimizer/report` | Latest Report |
| `POST` | `/api/optimizer/report/{report_id}/rec/{rec_id}/apply` | Apply Rec |
| `POST` | `/api/optimizer/report/{report_id}/rec/{rec_id}/dismiss` | Dismiss Rec |
| `GET` | `/api/optimizer/summary` | Optimizer Summary |

## panic (2)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/admin/panic` | Panic Global |
| `POST` | `/api/panic` | Panic User |

## partners (4)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/partners/brokers` | List Partner Brokers |
| `POST` | `/api/partners/brokers` | Upsert Partner Broker |
| `DELETE` | `/api/partners/brokers/{bid}` | Delete Partner Broker |
| `POST` | `/api/partners/brokers/{bid}/click` | Track Broker Click |

## performance (3)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/performance/share` | Create Share |
| `DELETE` | `/api/performance/share` | Revoke Share |
| `GET` | `/api/performance/verified` | Verified |

## portfolio (2)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/portfolio/deleverage` | Trigger Deleverage |
| `GET` | `/api/portfolio/snapshot` | Portfolio Snapshot |

## postmortem (11)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/postmortem` | List Postmortems |
| `GET` | `/api/postmortem/adjustments` | List Adjustments |
| `GET` | `/api/postmortem/guards` | List Guards |
| `POST` | `/api/postmortem/guards/{guard_id}/revert` | Revert Guard |
| `GET` | `/api/postmortem/patterns` | List Patterns |
| `GET` | `/api/postmortem/reviews` | List Reviews |
| `POST` | `/api/postmortem/reviews/run` | Run Review Now |
| `GET` | `/api/postmortem/settings` | Get Settings |
| `POST` | `/api/postmortem/settings` | Set Settings |
| `GET` | `/api/postmortem/{trade_id}` | Get Postmortem |
| `POST` | `/api/postmortem/{trade_id}/regenerate` | Regenerate |

## posture (1)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/bot/posture` | Market Posture |

## public-performance (1)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/public/performance/{share_id}` | Public Performance |

## quant (5)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/quant/allocator` | Allocator Weights |
| `POST` | `/api/quant/allocator/mode` | Set Allocator Mode |
| `GET` | `/api/quant/bayes/proposals` | Bayes Proposals |
| `POST` | `/api/quant/bayes/run` | Bayes Run |
| `GET` | `/api/quant/portfolio-optimization` | Portfolio Optimization |

## research-agent (8)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/research/auto-accept` | Get Auto Accept |
| `POST` | `/api/research/auto-accept` | Set Auto Accept |
| `GET` | `/api/research/last-run` | Last Run |
| `GET` | `/api/research/proposals` | List Proposals |
| `POST` | `/api/research/proposals/{proposal_id}/accept` | Accept Proposal |
| `POST` | `/api/research/proposals/{proposal_id}/dismiss` | Dismiss Proposal |
| `GET` | `/api/research/proposals/{proposal_id}/targets` | List Proposal Targets |
| `POST` | `/api/research/run` | Manual Run |

## rl (2)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/rl/policy` | Get Rl Policy |
| `POST` | `/api/rl/train` | Retrain Rl Policy |

## safety-blocks (5)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/safety-blocks/apply-suggestion` | Apply Suggestion |
| `GET` | `/api/safety-blocks/list` | List Blocks |
| `GET` | `/api/safety-blocks/stats` | Block Stats |
| `GET` | `/api/safety-blocks/suggestion` | Get Suggestion |
| `GET` | `/api/safety-blocks/{block_id}` | Block Detail |

## scalp (10)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/scalp/broker-stats` | Broker Stats Summary |
| `POST` | `/api/scalp/config` | Set Config |
| `DELETE` | `/api/scalp/config` | Remove Config |
| `GET` | `/api/scalp/decisions` | Decisions |
| `GET` | `/api/scalp/exec-calibration` | Exec Calibration |
| `GET` | `/api/scalp/executions` | Scalp Executions |
| `GET` | `/api/scalp/metrics` | Metrics |
| `POST` | `/api/scalp/retrain` | Retrain |
| `GET` | `/api/scalp/review` | Scalp Review |
| `GET` | `/api/scalp/status` | Status |

## sentiment (1)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/sentiment/{symbol}` | Sentiment For |

## settings (2)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/settings/preferences` | Get Preferences |
| `POST` | `/api/settings/preferences` | Set Preferences |

## setup (3)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/setup/claim-pairing` | Claim Pairing Token |
| `GET` | `/api/setup/pairing-status/{account_id}` | Pairing Status |
| `POST` | `/api/setup/pairing-token` | Issue Pairing Token |

## shadow (6)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/shadow/models` | List Shadow Models |
| `POST` | `/api/shadow/models/register` | Register Shadow Model |
| `POST` | `/api/shadow/models/{model_id}/promote` | Promote Shadow Model |
| `POST` | `/api/shadow/models/{model_id}/retire` | Retire Shadow Model |
| `GET` | `/api/shadow/performance` | Shadow Performance |
| `GET` | `/api/shadow/reconciliation` | Fill Reconciliation |

## signals (6)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/signals` | List Signals |
| `DELETE` | `/api/signals` | Bulk Clear Signals |
| `POST` | `/api/signals/generate` | Generate Signal |
| `POST` | `/api/signals/generate-all` | Generate All |
| `GET` | `/api/signals/watch-status` | Watch Status |
| `DELETE` | `/api/signals/{signal_id}` | Delete Signal |

## strategies (3)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/strategies` | List Strategies |
| `POST` | `/api/strategies` | Save Strategy |
| `DELETE` | `/api/strategies/{strategy_id}` | Delete Strategy |

## subscription (6)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/subscription/checkout` | Create Checkout |
| `GET` | `/api/subscription/plans` | List Plans |
| `GET` | `/api/subscription/poll/{session_id}` | Poll Session |
| `GET` | `/api/subscription/status` | Status |
| `GET` | `/api/subscription/transactions` | List Transactions |
| `POST` | `/api/webhook/stripe` | Stripe Webhook |

## telegram (4)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/telegram/incoming/{secret}` | Telegram Webhook |
| `POST` | `/api/telegram/webhook/disable` | Disable Webhook |
| `POST` | `/api/telegram/webhook/enable` | Enable Webhook |
| `GET` | `/api/telegram/webhook/status` | Webhook Status |

## trace (1)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/trace/{trace_id}` | Get Trace |

## trades (17)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/trades` | List Trades |
| `DELETE` | `/api/trades` | Bulk Clear Trades |
| `GET` | `/api/trades/ablation` | Gate Ablation |
| `GET` | `/api/trades/calibration` | Calibration Report |
| `GET` | `/api/trades/decisions` | List Trade Decisions |
| `GET` | `/api/trades/events` | Trade Lifecycle Events |
| `POST` | `/api/trades/execute/{signal_id}` | Execute Signal |
| `GET` | `/api/trades/history` | Trade History |
| `GET` | `/api/trades/live` | Live Open Trades |
| `POST` | `/api/trades/manual` | Execute Manual Trade |
| `POST` | `/api/trades/reconcile` | Reconcile Open Trades |
| `GET` | `/api/trades/scoreboard` | Strategy Scoreboard |
| `GET` | `/api/trades/stats` | Trade Stats |
| `GET` | `/api/trades/{trade_id}/audit` | Trade Audit |
| `POST` | `/api/trades/{trade_id}/close` | Close Trade |
| `GET` | `/api/trades/{trade_id}/explain` | Trade Explain |
| `POST` | `/api/trades/{trade_id}/revive` | Revive Trade |

