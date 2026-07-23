# Rollback Procedure

## Application (backend/frontend)
1. Identify the last good release (git tag / container digest).
2. Redeploy that artifact (platform rollback, or `docker compose up -d` with
   the pinned image digest).
3. Rollbacks are **schema-additive safe**: newer code only adds optional
   fields (`position_id`, `position_volume`, `partial_fill`, ...). Older code
   ignores them — no migration reversal needed.
4. After rollback, verify: `GET /api/health`, login, Trading Safety banner,
   `GET /api/bot/execution-health`.

## EA rollback
1. The backend accepts any EA >= `FENCING_MIN_EA` (currently 1.50). Rolling
   back the EA below the advertised LATEST is safe as long as it stays >= the
   fencing floor.
2. NEVER roll the EA back below 1.50 — pre-fencing EAs ignore sequence
   fencing and can execute superseded commands.
3. To roll back: recompile the previous tagged `.mq5` in MetaEditor, replace
   the EA on the chart; the journal format is backward compatible (v1.52+
   split tickets fall back to legacy single-value reads).

## Emergency stop (independent of rollback)
1. Dashboard → panic/disable bots (or `POST /api/nl/command` "disable all bots"
   — disable is executed immediately, it is not gated behind proposals).
2. Verify outbox drains and no new dispatches occur.
3. Open positions remain protected by broker-side stops (verify UNPROT badge
   count is zero on the Trades page).
