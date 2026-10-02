# STOIC — Cloudflare Edge Security Runbook (Free plan)

Target architecture: **Internet → Cloudflare (DNS / CDN / WAF / DDoS / Rate
Limiting / Bot Fight / Turnstile / Access / Tunnel) → Origin (FastAPI +
MongoDB)**. Everything below is achievable on the **Free** plan unless marked
otherwise; where a paid feature exists, the free-tier alternative is given.

The app-side half is already implemented in code (iter-155):
- Turnstile verification on login / register / password-reset (admin toggle:
  `/admin/users` → "Cloudflare Turnstile", API `/api/admin/settings/turnstile`)
- Security headers on all API responses (HSTS, CSP, XFO, Referrer-Policy,
  Permissions-Policy, COOP/CORP)
- `CF-Connecting-IP` support (`TRUST_CF_CONNECTING_IP=true` in prod env)
- Session idle timeout (`SESSION_IDLE_TIMEOUT_MINUTES`) + new-IP login
  notification emails
- Refresh-token rotation with reuse detection, plus a concurrent-refresh grace
  window (`REFRESH_REUSE_GRACE_SECONDS`, default 60): two tabs whose access
  tokens expire together no longer trip "reuse detected" and log the user out;
  a token with a different hash, or re-presented after the window, still
  revokes the whole family. Consumption is atomic (`find_one_and_update`), so
  parallel refreshes mint exactly one live successor.
- Existing layers: JWT cookies, per-endpoint app rate limits, step-up MFA,
  HMAC-signed host-agent commands, Ed25519-signed artifacts, audit chain

---

## 1. DNS + Proxy (prerequisite)
Dashboard → DNS. Every public record (`stoicaibot.com`, `www`) must be
**Proxied (orange cloud)** — grey-cloud records bypass ALL Cloudflare
protection and leak the origin IP.

- SSL/TLS → Overview → **Full (strict)**. Never "Flexible".
- SSL/TLS → Edge Certificates → enable **Always Use HTTPS**, **Minimum TLS
  1.2**, **TLS 1.3**, **Automatic HTTPS Rewrites**.
- Enable **HSTS** here (Edge Certificates → HSTS): max-age 12 months,
  includeSubDomains, preload — matches the header the API already sends.

## 2. WAF (Security → WAF)
Free plan includes the **Cloudflare Free Managed Ruleset** (high-confidence
SQLi/XSS/RCE + emergency CVE rules) — verify it shows **Enabled**.

Free plan also allows **5 custom rules**. Recommended set (in order):

| # | Name | Expression | Action |
|---|------|-----------|--------|
| 1 | Block non-CF ports probing | `(not ssl)` | Block |
| 2 | Block sensitive path scans | `http.request.uri.path in {"/.env" "/.git/config" "/wp-login.php" "/xmlrpc.php" "/phpmyadmin"} or http.request.uri.path contains "/.git/"` | Block |
| 3 | Challenge suspicious login geo | `http.request.uri.path eq "/api/auth/login" and not ip.src.country in {"NO" "SE" "DK" "FI" "DE" "GB" "US"}` | Managed Challenge *(edit country list to your market; Managed Challenge — never Block — so travelling users pass)* |
| 4 | Block admin surface from bad ASNs | `starts_with(http.request.uri.path, "/api/admin/") and cf.threat_score gt 20` | Managed Challenge |
| 5 | Protect bridge endpoints from browsers | `starts_with(http.request.uri.path, "/api/bridge/") and http.user_agent contains "Mozilla"` | Managed Challenge *(EAs/agents never send browser UAs)* |

## 3. Rate Limiting (Security → WAF → Rate limiting rules)
Free plan includes **1 rate limiting rule** (10s window). App-level limits
already cover login lockout (5 fails/10 min), register (30/h), pw-reset
(20/h) — so spend the single edge rule on the broadest surface:

- **Rule: API flood guard** — expression
  `starts_with(http.request.uri.path, "/api/")`, characteristic: IP,
  requests: **50 per 10 seconds**, action: Block for default duration.

> Pro plan upgrade path (when ready): add dedicated rules —
> `/api/auth/login` 5/min, `/api/auth/register` 3/min,
> `/api/auth/forgot-password` 2/hour, `/api/*` 100/min — then keep the app
> limits as second layer.

## 4. Bot protection (Security → Bots)
- Enable **Bot Fight Mode** (free): challenges definite bots, blocks AI
  crawlers. Note: it cannot be scoped per-path on free; if it ever
  interferes with the MT5 EA or host agent (non-browser clients hitting
  `/api/bridge/*`), create a WAF custom-rule **Skip** for
  `starts_with(http.request.uri.path, "/api/bridge/")` verified by the
  bridge token, or upgrade to Super Bot Fight Mode (Pro) for granularity.
- Block **AI Scrapers and Crawlers** toggle: ON.

## 5. Turnstile (already coded — enable it)
1. Dashboard → Turnstile → your widget → **Hostnames**: add
   `stoicaibot.com`, `www.stoicaibot.com` (add the preview hostname only on
   a separate non-production widget).
2. Production deploy env must contain `TURNSTILE_SITE_KEY` +
   `TURNSTILE_SECRET_KEY` (already in backend/.env for this build).
3. In the app: `/admin/users` → "Cloudflare Turnstile" → ENABLE. Login,
   registration and password-reset now require the challenge; the backend
   verifies every token server-side (single-use, action + hostname bound,
   300s freshness) and is **fail-closed in every state** — a Cloudflare
   outage blocks the surface (login may fall back to an emailed one-time
   code only with `TURNSTILE_LOGIN_DEGRADED_POLICY=otp_required`). Also set
   `TURNSTILE_EXPECTED_HOSTNAMES=stoicaibot.com,www.stoicaibot.com`.
   Full state machine + break-glass runbook: `docs/TURNSTILE.md`.

## 6. Zero Trust Access for admin surfaces (free ≤ 50 users)
Zero Trust dashboard → Access → Applications → Add → Self-hosted:
- Application domain: `stoicaibot.com/admin*` — covers `/admin/users`,
  `/admin/ops`, `/admin/brokers`, `/admin/support`, `/admin/runbooks`, etc.
  Add a second app for `stoicaibot.com/api/admin*` (API layer).
- Policy: Allow → Include → Emails: your admin email list. Session: 24h.
- Login method: One-time PIN (free) or add Google as an IdP.
This puts Cloudflare-managed SSO **in front of** the app's own admin-role +
TOTP enforcement — three independent gates on every admin surface.
> If any separate internal tools exist later (Grafana, Mongo Express, CI
> dashboards), NEVER expose them publicly — publish each through the Tunnel
> (below) on its own subdomain with an Access policy.

## 7. Cloudflare Tunnel (origin cloaking)
On the origin host:
```bash
# install cloudflared, then:
cloudflared tunnel login
cloudflared tunnel create stoic-prod
cloudflared tunnel route dns stoic-prod www.stoicaibot.com
```
`/etc/cloudflared/config.yml`:
```yaml
tunnel: <TUNNEL_ID>
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: www.stoicaibot.com
    service: http://localhost:80     # frontend
  - hostname: www.stoicaibot.com
    path: /api/.*
    service: http://localhost:8001   # FastAPI
  - service: http_status:404
```
```bash
cloudflared service install && systemctl enable --now cloudflared
```
Then **close ALL inbound firewall ports** (80/443 included) — the tunnel is
outbound-only. Once the origin is only reachable through Cloudflare, set
`TRUST_CF_CONNECTING_IP=true` in the production env so rate limiting, audit
logs and login alerts key on the real client IP.
> If the app stays on managed hosting (Emergent), the platform terminates
> traffic — the Tunnel applies when self-hosting the origin (VPS phase).

## 8. Security headers at the edge (Rules → Transform Rules → Modify Response Header)
The API already sends the full header set; add the same for the HTML/static
responses (free plan: 10 transform rules). **Scope the rule to non-`/api/*`
paths (or use "Set static" = overwrite, never "Add")** — the live audit (round
19, SEC-002) observed doubled `Strict-Transport-Security` (two different
max-ages), `Referrer-Policy` and `X-Content-Type-Options` on `/api/status`
because both the origin and the edge appended them. Each header must be set in
exactly ONE layer with ONE HSTS policy (`max-age=31536000; includeSubDomains;
preload` — the dashboard HSTS toggle must use the same 12-month value):
- `Strict-Transport-Security: max-age=31536000; includeSubDomains; preload`
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: geolocation=(), microphone=(), camera=(), payment=(), usb=()`
- `Content-Security-Policy: default-src 'self'; script-src 'self' https://challenges.cloudflare.com; frame-src https://challenges.cloudflare.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com data:; img-src 'self' data: https:; connect-src 'self' https://www.stoicaibot.com https://challenges.cloudflare.com`
Validate with Mozilla Observatory (https://observatory.mozilla.org) after
applying — target grade A. Adjust `connect-src`/`script-src` if new
third-party scripts (e.g. Stripe.js) are added.

## 9. DDoS
Security → DDoS: the managed L3/4/7 rulesets are **always on** for proxied
records on every plan — set sensitivity to High and action Managed
Challenge for the HTTP DDoS ruleset override. Nothing else required; the
main risk is a grey-clouded record leaking the origin IP (see §1/§7).

## 10. API protection for machine clients (API Shield alternative)
Full mTLS API Shield is Enterprise; STOIC already has equivalent app-layer
controls — keep them authoritative:
- MT5 EA / bridge: per-account rotating `bridge_token`, identity
  verification chain, HMAC-signed command queue with monotonic seq.
- Host Agent: short-lived pairing tokens + Ed25519-verified artifacts.
- Optional free upgrade: issue **Access Service Tokens** (Zero Trust →
  Access → Service Auth) for the host agent and require them on
  `/api/infra/agent/*` via an Access application — Cloudflare then rejects
  unauthenticated machines before the origin sees them.

## 11. Monitoring
- Analytics & Logs → Security: review blocked/challenged events weekly
  (attack sources, top paths). Traffic: cache ratio + bandwidth.
- Security → Events: filter by rule to tune the custom rules from §2
  (false-positive check the geo rule during the first week).
- In-app: `/admin/ops` Ops Console already tracks API latency, auth
  failures and alerting; Cloudflare analytics covers the edge half.

## Rollout order (safe sequence)
1. §1 DNS/TLS hygiene → 2. §2 WAF managed + custom rules (deploy in **Log**
   action for 24h first where unsure, then flip to Block/Challenge) →
3. §3 rate limit rule → 4. §4 Bot Fight Mode →
5. §5 enable Turnstile in-app → 6. §6 Access on /admin* →
7. §7 Tunnel + `TRUST_CF_CONNECTING_IP=true` (self-hosted phase) →
8. §8 headers + Observatory validation → 9. weekly §11 review.

Each step is independently reversible; none require app downtime.
