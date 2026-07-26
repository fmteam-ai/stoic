"""STOIC — Privacy Policy + Risk Disclosure (canonical sources).

Served by GET /api/legal/{privacy|risk}. Same pattern as terms_of_use.py.
NOTE: drafted texts — the owner should have legal counsel review before
relying on them in any jurisdiction.
"""

PRIVACY_VERSION = "2026-06-01"
RISK_VERSION = "2026-06-01"
LAST_UPDATED = "June 1, 2026"

PRIVACY_MARKDOWN = """# STOIC — Privacy Policy

**Version:** {version}
**Last Updated:** {last_updated}

This Privacy Policy explains what data STOIC ("we", "us") collects when you
use the STOIC trading platform (the "Service"), why we collect it, and the
rights you have over it.

---

## 1. Data We Collect

- **Account data** — email address, display name, hashed password (bcrypt —
  we can never read your password), acceptance timestamps for the Terms of
  Use, and your subscription/entitlement state.
- **Security data** — login timestamps, IP-derived rate-limit counters,
  two-factor enrollment state (TOTP secrets are stored encrypted and never
  displayed again), security audit trail entries, and one-time sign-in codes
  (stored only as salted hashes, deleted on use or expiry).
- **Trading data** — the MT5 account numbers and broker server names you
  register, balance/equity snapshots and open-position data reported by the
  STOIC Expert Advisor ("EA") you install, trade history your EA syncs, and
  the bot configurations you create.
- **Billing data** — your subscription plan, payment amounts and Stripe
  Checkout session identifiers. **We never see or store your card number** —
  payment details are handled entirely by Stripe.
- **Support data** — tickets and messages you submit through the in-app
  support system.

## 2. What We Never Collect

- Your MT5 **investor or master passwords**. The EA runs inside your own
  terminal; STOIC authenticates it with a per-account bridge token.
- Your card or bank details (Stripe processes payments).
- Data from your device beyond what is needed to render the app.

## 3. How We Use Your Data

- To operate the Service: generate signals, route trades to your EA, enforce
  risk guardrails, and show you your own performance analytics.
- To secure your account: rate limiting, anomaly detection, two-factor and
  email verification, session revocation.
- To bill you and to honor refunds/disputes via Stripe.
- To send transactional email (activation, password reset, sign-in codes,
  renewal reminders, support replies). We do not send marketing email
  without a separate opt-in.
- To improve the bot's decision quality using **your own** trade outcomes.
  Model training is scoped per user — your trading data is not used to train
  models served to other users.

## 4. Legal Bases (GDPR)

Where the GDPR applies, we process data under: performance of a contract
(operating your account), legitimate interest (security, fraud prevention),
legal obligation (billing records), and consent (optional notifications).

## 5. Sharing

We share data only with processors required to run the Service: Stripe
(payments), Resend (transactional email), our hosting provider (encrypted
infrastructure), and — only if you connect them — Telegram (notifications you
enable). We never sell personal data.

## 6. Retention

- Account and trading data: kept while your account is active.
- Billing ledger entries: retained as required for accounting.
- Sign-in codes: minutes (deleted on use/expiry). Sessions: revoked server-side
  on logout and rotate automatically.
- On verified account-deletion requests we delete or irreversibly anonymize
  personal data within 30 days, except records we must keep by law.

## 7. Your Rights

Depending on your jurisdiction you may request: access to your data, a
portable copy, correction, deletion, restriction of processing, or objection
to processing. Submit requests through the in-app Support page (category
"Account") — we verify the request against your authenticated session.

## 8. Security

Passwords are bcrypt-hashed and screened against known breach corpora;
sessions live in httpOnly cookies with CSRF protection and server-side
revocation; live-sensitive actions require step-up MFA; EA artifacts are
integrity-signed. No system is perfectly secure — report suspected issues
via Support immediately.

## 9. International Transfers

Infrastructure may be located outside your country. Where required, we rely
on appropriate safeguards (e.g. standard contractual clauses of our
processors).

## 10. Children

The Service is not directed at anyone under 18. We do not knowingly collect
data from minors.

## 11. Changes

We will update the version stamp above and notify you in-app of material
changes. Continued use after the effective date constitutes acceptance.

## 12. Contact

Privacy requests: open a Support ticket (category "Account") or email the
address on the Status page footer.
""".format(version=PRIVACY_VERSION, last_updated=LAST_UPDATED)


RISK_MARKDOWN = """# STOIC — Risk Disclosure Statement

**Version:** {version}
**Last Updated:** {last_updated}

**Read this before enabling live trading.** By activating any live trading
mode you acknowledge every statement below.

---

## 1. You Can Lose Money — Including Everything You Deposit

Trading leveraged products (gold, indices, FX, crypto CFDs) carries a high
level of risk. Losses can exceed your invested capital with some brokers.
**Never trade with money you cannot afford to lose.**

## 2. STOIC Is Software, Not a Broker or Advisor

- STOIC is a software tool that automates order routing to **your own**
  brokerage account according to rules **you** enable.
- Nothing in the Service — signals, confidence scores, AI explanations,
  post-mortems, forecasts, or documentation — is investment advice, a
  recommendation, or a solicitation to trade.
- We are not a broker-dealer, investment adviser, or fiduciary, and we do
  not hold your funds at any time. Your funds stay with your broker.

## 3. Past Performance Is Not Indicative of Future Results

Backtests, shadow-mode results, demo campaigns, testimonials and verified
performance pages describe historical outcomes under specific market
conditions. They do not predict future returns. Win rates change with
market regimes.

## 4. Automated Trading Has Specific Risks

- **Technology risk** — VPS outages, broker API failures, EA disconnects,
  or platform bugs can prevent orders (including protective stops) from
  being placed, modified, or closed in time.
- **Execution risk** — slippage, spread widening (especially around news and
  the daily close), requotes and partial fills can materially worsen results
  versus the signaled price.
- **Model risk** — AI/ML models can be confidently wrong, especially in
  regimes unlike their training data. Guardrails reduce but never eliminate
  this risk.
- **Leverage risk** — small market moves produce large P&L swings; margin
  calls can close positions at the worst moment.

## 5. Safety Systems Are Mitigations, Not Guarantees

Drawdown ladders, exposure caps, kill-switches, anti-tilt cooldowns and the
veto cascade are designed to reduce risk. They can fail, be bypassed by
broker-side events, or simply be insufficient in extreme markets (gaps,
flash crashes, liquidity vacuums).

## 6. Demo First, Small Second

We strongly recommend: run Demo/Shadow mode until you understand the bot's
behavior; graduate to Supervised Live with the minimum position sizing; only
then consider higher risk profiles. Autonomous mode requires certification
for a reason.

## 7. Your Responsibilities

You are solely responsible for: broker selection, account configuration,
risk settings, tax obligations, legal eligibility to trade leveraged
products in your jurisdiction, and monitoring your account. Check your
broker's regulatory status and negative-balance-protection policy.

## 8. No Liability for Trading Losses

To the maximum extent permitted by law, STOIC and its operators are not
liable for trading losses, missed profits, or damages arising from use of
the Service, as further detailed in the Terms of Use.

---

**If you do not fully understand these risks, do not enable live trading.
Consider consulting an independent licensed financial adviser.**
""".format(version=RISK_VERSION, last_updated=LAST_UPDATED)


def get_legal(kind: str) -> dict | None:
    if kind == "privacy":
        return {"kind": "privacy", "version": PRIVACY_VERSION,
                "last_updated": LAST_UPDATED, "markdown": PRIVACY_MARKDOWN}
    if kind == "risk":
        return {"kind": "risk", "version": RISK_VERSION,
                "last_updated": LAST_UPDATED, "markdown": RISK_MARKDOWN}
    return None
