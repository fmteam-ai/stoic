# STOIC engineering entry points — every lane is one command.
.PHONY: test-unit test-integration test-full lock lock-check verify-ea manifest publish rollback status backup migrator-on migrator-off

test-unit:            ## reproducible 516-test unit lane (env doctor included)
	./scripts/test_unit.sh

test-integration:     ## integration lane (needs MongoDB via backend/.env)
	cd backend && python -m pytest tests/integration -q

test-full:            ## full classified suite against a running stack
	STOIC_ALLOW_MUTATING_TESTS=YES ./scripts/run_full_suite.sh

lock:                 ## freeze the RC dependency lock (release/rc_lock.json)
	python scripts/freeze_rc_lock.py

lock-check:           ## fail if the current environment drifted from the lock
	python scripts/freeze_rc_lock.py --check

verify-ea:            ## validate the signed EX5 release entry (see --help)
	python scripts/verify_ea_release.py --check

manifest:             ## regenerate docs/TEST_MANIFEST.md
	python scripts/generate_test_manifest.py

# ── server operations (run ON the server, inside the checkout) ──────────────
publish:              ## re-publish latest origin/main (or REF=v1.2.3): backup → build → verify → auto-rollback
	deploy/update.sh $(REF)

rollback:             ## roll back to the previous verified release (or REF=<tag|sha>)
	deploy/rollback.sh $(REF)

status:               ## containers, running build SHA, last releases
	@docker compose ps --format '{{.Name}}\t{{.Status}}'
	@curl -fsS http://127.0.0.1:8001/api/health | python3 -c 'import sys,json;d=json.load(sys.stdin);print("build_sha:",d.get("build_sha"),"| policy:",d.get("execution_policy_version"),"| ea:",d.get("ea_version"))'
	@tail -3 deploy/releases.log 2>/dev/null || true

backup:               ## timestamped Mongo backup into ./backups
	deploy/backup.sh backup

# ── host migration (admin wizard at /admin/host-migration) ──────────────────
migrator-on:          ## enable the migration sidecar (docker socket + SSH) next to the stack
	@. deploy/lib.sh; set_kv .env STOIC_ROOT "$$(pwd)"; \
	CF=$$(grep '^COMPOSE_FILE=' .env | cut -d= -f2-); [ -n "$$CF" ] || CF=docker-compose.yml; \
	case ":$$CF:" in *:docker-compose.migrator.yml:*) ;; *) set_kv .env COMPOSE_FILE "$$CF:docker-compose.migrator.yml";; esac
	docker compose up -d --build migrator backend
	@echo "migrator enabled — open /admin/host-migration. Disable afterwards with: make migrator-off"

migrator-off:         ## remove the migration sidecar again
	docker compose stop migrator 2>/dev/null || true; docker compose rm -f migrator 2>/dev/null || true
	@. deploy/lib.sh; CF=$$(grep '^COMPOSE_FILE=' .env | cut -d= -f2- | sed 's/:docker-compose.migrator.yml//'); set_kv .env COMPOSE_FILE "$$CF"
	docker compose up -d backend
