# STOIC engineering entry points — every lane is one command.
.PHONY: test-unit test-integration test-full lock lock-check verify-ea manifest

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
