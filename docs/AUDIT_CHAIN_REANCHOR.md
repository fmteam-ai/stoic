# Audit chain — concurrency fix and safe re-anchor (v1.60.41)

**Incident (15 Oct 11:46:58):** 8 `admin_audit_log` appends within ~120 ms raced in `audit_chain.append_chained`
(read head → compute seq → insert, no serialisation). Two writers read the same head, so seq 77 and 78 were each
written twice with the same `prev_hash`; `verify_chain` reported `chain_link_broken` → CRITICAL **I3 "Audit chain broken"**.
Every entry's *content* is intact — only the links/sequence forked.

## Fix (code)
- `append_chained` now runs under an in-process `asyncio.Lock` **and** a unique partial index
  `uniq_chain_seq` on `seq` (chained entries only). A cross-process race surfaces as `DuplicateKeyError` and is
  retried from a fresh head (≤ 12 attempts, jittered); it never writes a duplicate and fails loudly if it cannot win.
- The index is built at startup (`seed.py` → `audit_chain.ensure_seq_index`) and required by security check **I2**
  (`admin_audit_log: uniq_chain_seq`). It **cannot** be built while duplicate seqs exist — repair first (below).
- `verify_chain` returns `first_bad_seq`, which I3 now prints.

## Safe re-anchor of the existing break (operator)
```
deploy/reanchor-audit-chain.sh                                           # 1. verify + DRY-RUN plan — writes nothing
deploy/reanchor-audit-chain.sh --apply --actor ops@… \
       --reason "15 Oct 11:46:58 seq race (dup 77/78)"                    # 2. apply
```
The wrapper runs the script **inside the backend image** (`docker compose exec -T backend python
/app/scripts/reanchor_audit_chain.py …`, shipped since v1.60.43; for older images it pipes the checked-out script
in) — the host's Python 3.9 has neither the backend's libraries nor `MONGO_URL`. Running
`python scripts/reanchor_audit_chain.py` directly only works where the backend's Python and `backend/.env` are available.
What `--apply` does, in order (all inside `audit_chain.repair_chain`):
1. Refuses if any entry's content no longer matches its own `entry_hash` (that is tampering, not a race — restore from backup).
2. Snapshots the original documents of every entry from the break onward into `audit_chain_repairs`
   (`repair_id`, actor, reason, `break_seq`, `old_head`, `originals[]`).
3. Re-links from the break in (seq, insertion) order: new contiguous `seq`, `prev_hash` = previous `entry_hash`,
   recomputed `entry_hash`. Each rewritten entry keeps `reanchor: {orig_seq, orig_prev_hash, orig_entry_hash, repair_id}`
   — covered by its new hash, so the repair is visible in the chain itself. Content fields are untouched.
4. Appends a chained `audit_chain_reanchored` marker (repair_id, reason, break_seq, entries_rewritten, old_head).
5. Marks anchors at/after the break `superseded_by_repair`, builds `uniq_chain_seq`, and creates a new signed anchor
   at the new head (`audit_anchor.create_anchor`; falls back to `POST /api/ops/audit-anchor` if the signer is unavailable).
6. Prints `verify_chain` and `verify_latest` after — both must be `ok: true`; I3 clears on its next run (hourly), I2 once the index exists.

Idempotent: a second `--apply` on an intact chain reports `chain intact` and writes nothing.
