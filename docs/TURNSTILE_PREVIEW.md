# Turnstile in preview (audit round 13 P2-04)

- **Preview is intentionally Turnstile-DISABLED** (`TURNSTILE_FORCE_DISABLE` / no site key in the preview env) because the
  preview hostname is ephemeral and cannot be placed on a Cloudflare hostname allowlist ahead of time; edge bot
  protection remains active on the preview ingress but is NOT a substitute for per-action Turnstile binding.
- **Staging/production MUST run Turnstile enabled** with `TURNSTILE_EXPECTED_HOSTNAMES` + per-action binding
  (`login`, `register`, `password_reset`); production refuses to boot without a hostname allowlist
  (`turnstile_gate.expected_hostnames`, round 10 P1-04).
- Server-side behaviour is verified hermetically (no CAPTCHA interaction needed): valid-once, replay, wrong
  action/hostname, stale/future timestamp, every malformed claim type (`typed_claims`), provider outage → OTP only under
  server-observed degradation, client-script failure stays locked (tests: test_iter239_round10, test_iter242_round12).
- Cloudflare challenge completion/recovery and abuse-rate challenge behaviour are validated in the sanctioned staging
  acceptance run (`scripts/staging_acceptance.sh`), not against the preview.
