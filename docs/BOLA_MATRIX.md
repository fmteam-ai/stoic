# BOLA Authorization Matrix

Auto-generated from `backend/security_matrix.py`. Every route accepting `account_id`/`bot_id` is declared with its ownership enforcement; the test suite fails when a sensitive route is missing.

| Method | Path | Enforcement |
|--------|------|-------------|
| PATCH | `/api/accounts/{account_id}` | user_scoped_query |
| DELETE | `/api/accounts/{account_id}` | user_scoped_query |
| GET | `/api/accounts/{account_id}/block-status` | user_scoped_query |
| POST | `/api/accounts/{account_id}/certify` | user_scoped_query |
| PATCH | `/api/accounts/{account_id}/credentials` | user_scoped_query |
| POST | `/api/accounts/{account_id}/credentials/reveal` | user_scoped_query |
| POST | `/api/accounts/{account_id}/import-positions` | user_scoped_query |
| POST | `/api/accounts/{account_id}/request-sync` | user_scoped_query |
| POST | `/api/accounts/{account_id}/rotate-token` | user_scoped_query |
| PUT | `/api/accounts/{account_id}/symbol-suffix` | user_scoped_query |
| GET | `/api/accounts/{account_id}/test-connection` | user_scoped_query |
| POST | `/api/accounts/{account_id}/test-trade` | user_scoped_query |
| POST | `/api/accounts/{account_id}/unblock` | user_scoped_query |
| GET | `/api/bot/adaptive-status` | user_scoped_query |
| GET | `/api/bot/config` | user_scoped_query |
| PUT | `/api/bot/config` | user_scoped_query |
| DELETE | `/api/bot/config` | user_scoped_query |
| GET | `/api/bot/doctor` | user_scoped_query |
| POST | `/api/bot/my-presets` | user_scoped_query |
| POST | `/api/bot/preset/{key}` | user_scoped_query |
| GET | `/api/bot/sizing-preview` | user_scoped_query |
| POST | `/api/bot/start` | user_scoped_query |
| GET | `/api/bot/status` | user_scoped_query |
| POST | `/api/bot/stop` | user_scoped_query |
| GET | `/api/brain/costs` | owned_account_helper |
| GET | `/api/brain/health` | owned_account_helper |
| GET | `/api/brain/portfolio` | owned_account_helper |
| GET | `/api/brain/regime` | owned_account_helper |
| GET | `/api/broker-intel/forecast` | user_scoped_query |
| POST | `/api/config/rollback` | user_scoped_query |
| GET | `/api/config/versions` | user_scoped_query |
| DELETE | `/api/crypto/accounts/{account_id}` | user_scoped_query |
| GET | `/api/crypto/accounts/{account_id}/balance` | user_scoped_query |
| POST | `/api/crypto/accounts/{account_id}/execute` | user_scoped_query |
| GET | `/api/crypto/accounts/{account_id}/ticker` | user_scoped_query |
| POST | `/api/crypto/accounts/{account_id}/verify` | user_scoped_query |
| GET | `/api/execution/quality` | user_scoped_query |
| POST | `/api/optimizer/analyze` | user_scoped_query |
| GET | `/api/optimizer/report` | user_scoped_query |
| GET | `/api/portfolio/snapshot` | user_scoped_query |
| GET | `/api/risk/budget` | user_scoped_query |
| GET | `/api/risk/layers` | user_scoped_query |
| GET | `/api/risk/strategy-portfolio` | user_scoped_query |
| DELETE | `/api/scalp/config` | user_scoped_query |
| GET | `/api/scalp/executions` | user_scoped_query |
| GET | `/api/scalp/metrics` | user_scoped_query |
| POST | `/api/scalp/retrain` | user_scoped_query |
| GET | `/api/scalp/status` | user_scoped_query |
| GET | `/api/setup/pairing-status/{account_id}` | user_scoped_query |
| POST | `/api/signals/generate` | user_scoped_query |
| POST | `/api/signals/generate-all` | user_scoped_query |
| GET | `/api/trades` | user_scoped_query |
| GET | `/api/trades/history` | user_scoped_query |
| GET | `/api/trades/live` | user_scoped_query |
| GET | `/api/trades/stats` | user_scoped_query |
| GET | `/api/v1/trades` | user_scoped_query |

