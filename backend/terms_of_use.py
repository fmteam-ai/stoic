"""STOIC — Terms of Use (canonical source).

The version + last_updated below are returned by GET /api/terms and are
displayed verbatim on the public /terms page. When a user registers, the
current TERMS_VERSION is stamped on their user doc as `accepted_terms_version`.

Updating the rules:
- Bump TERMS_VERSION (YYYY-MM-DD) whenever the substantive obligations change.
- Frontend will compare the user's stored version against the current one and
  prompt re-acceptance on next login (P1 follow-up — not enforced today).
"""

TERMS_VERSION = "2026-07-04"
LAST_UPDATED = "July 4, 2026"

TERMS_MARKDOWN = """# STOIC — Terms of Use

**Version:** {version}  
**Last Updated:** {last_updated}

By creating an account, subscribing to a plan, joining the affiliate program,
or using the STOIC trading bot (the "Service"), you ("User") agree to these
Terms of Use ("Terms"). If you do not agree, do not use the Service.

---

## 1. The Service

STOIC is a software-as-a-service trading automation platform that connects to
your own MetaTrader 5 ("MT5") brokerage accounts and/or supported crypto
exchanges (Binance, Kraken, OKX, KuCoin, Binance.US) via API, analyzes market
conditions using proprietary AI signal pipelines, and — when you explicitly
enable it — places trades on your behalf.

**STOIC is a tool. STOIC is not a broker, custodian, money manager, financial
advisor, or fiduciary.** You retain custody of your funds at all times. You
retain sole responsibility for every order the bot places under your
configuration.

## 2. No Financial Advice

Nothing in the Service constitutes financial, investment, legal, tax, or
trading advice. Signals, suggestions, post-mortems, AI explanations, and
adaptive recommendations are informational only and are produced by
algorithms that may be wrong.

## 3. Risk Acknowledgement

Trading leveraged instruments — including Gold (XAUUSD), Bitcoin (BTCUSD),
forex pairs, and crypto spot — carries substantial risk of loss. Past
performance, backtests, paper-shadow results, and live results of other
users are **not** indicative of future results. **You can lose all the
capital you allocate to the bot, and in margin/leveraged accounts you may
lose more than you deposit.** Use only risk capital you can afford to lose.

## 4. Eligibility & Compliance

You represent that you are:
1. At least 18 years old (or the age of majority in your jurisdiction).
2. Legally permitted to use automated trading software where you reside.
3. **Not located in, or a resident of, a jurisdiction where the use of this
   Service or the underlying exchanges is prohibited** (including but not
   limited to OFAC-sanctioned countries).
4. Solely responsible for any tax filings, reporting, and regulatory
   obligations arising from your trading activity.

## 5. Your Account

- One human, one account. Multi-account or shared-credential use without
  written permission is a violation.
- Keep your credentials, API keys, MT5 logins, and 2FA recovery codes
  secret. STOIC will never ask for them outside the in-app forms.
- You are responsible for all activity that occurs under your login.
- You must immediately notify support of any unauthorized access.

## 6. Acceptable Use

You agree **not** to:
1. **Abuse, reverse-engineer, decompile, or scrape** the platform, its APIs,
   the MT5 EA, or its underlying models.
2. **Resell, white-label, or sublicense** STOIC's signals, output, or
   downloadable EA without an explicit commercial agreement.
3. **Manipulate** the Service via fake trades, wash trading, latency
   exploitation, exchange-rule violations, or coordinated abuse with other
   accounts.
4. **Defraud the affiliate program** via self-referrals (using your own
   referral link to sign up), incentivized clicks, bot-driven clicks,
   chargeback abuse, false advertising, fake testimonials, or any form of
   misrepresentation of STOIC's performance.
5. **Operate the bot against accounts you do not own** or against accounts
   whose use violates the broker / exchange's own terms of service.
6. **Use STOIC to harm others**, including running coordinated market-moving
   campaigns or facilitating money laundering, terrorism financing, or any
   activity that is illegal in your or our jurisdiction.
7. **Bypass safety controls** — including but not limited to: tampering
   with the EA, modifying server-side risk caps, disabling the broker-reject
   circuit breaker on behalf of another user, or exploiting bugs without
   responsibly disclosing them.
8. **Impersonate** STOIC, its team, its affiliates, or other users in any
   communication or marketing material.
9. **Spam** other users, prospects, or our support channels.

## 7. Affiliate Program

Joining the affiliate program is governed by these Terms plus the in-app
Affiliate disclosures (commission rate, cookie window, payout minimums).
Affiliates additionally agree to:

- Promote STOIC only through channels and audiences they own or have
  permission to use. **No paid Google/Facebook ads bidding on the STOIC
  brand name** without prior written consent.
- Never make **earnings claims, performance guarantees, or risk-free
  promises** on STOIC's behalf. Always disclose that trading carries risk
  of loss.
- Never engage in **cookie stuffing, click fraud, incentivized signups,
  fake reviews, paid testimonials misrepresented as organic, or any form
  of deceptive marketing**.
- Disclose the affiliate relationship in every promotional post per local
  law (e.g., FTC "#ad" / "#affiliate" in the United States).
- Not target minors, sanctioned persons, or residents of restricted
  jurisdictions.

Violation of any affiliate clause may result in:
- Forfeiture of all unpaid commissions.
- Immediate suspension or termination of the affiliate account.
- Clawback of previously-paid commissions tied to fraudulent referrals.

## 8. Enforcement — Suspension & Termination

STOIC reserves the right, at our sole discretion and without prior notice,
to **suspend** or **terminate** any User account or Affiliate account that
we believe, in good faith, has violated these Terms.

- **Suspension** disables login, halts the bot from placing new trades on
  your behalf, freezes affiliate clicks/commission accrual, and blocks
  access to the dashboard. Existing open positions remain on your broker
  account — closing or managing them is your responsibility (you may
  continue to access your broker terminal directly).
- **Termination** is permanent. The account is closed, all bot configurations
  are deactivated, affiliate codes are deactivated, and unpaid commissions
  may be forfeited if the termination was for a fraud-based violation.
- For severe or fraudulent violations we may also report the activity to
  law enforcement, your broker/exchange, payment processors, and other
  affected parties.

You may appeal a suspension or termination by emailing the Support contact
listed in the Service. We will review appeals in good faith but our
decision is final.

## 9. Subscription & Payments

- Subscriptions auto-renew at the price displayed at checkout until
  cancelled. Cancellation stops future renewals; it does not refund the
  current period unless required by applicable law.
- We process payments through third-party processors (e.g., Stripe). We
  do not store full card numbers on our servers.
- Failed renewals trigger a 30-day grace period before downgrading to
  the Starter tier.

## 10. Refunds

Trading involves market risk; **we do not refund based on trading
performance** (yours or any other user's). We may issue goodwill credits
or refunds at our sole discretion for service outages or billing errors.

## 11. Intellectual Property

STOIC, the EA, the AI models, the dashboards, the documentation, the
trailer, the branding, and all related code remain the property of the
operator. You receive a personal, non-exclusive, non-transferable,
revocable license to use the Service while your subscription is active.

## 12. Privacy & Data

We process data per our Privacy Policy. We may aggregate anonymized
trade telemetry to improve the AI models. You may request export or
deletion of your personal data subject to applicable retention laws.

## 13. Liability

To the maximum extent permitted by law, the Service is provided **"AS
IS"** without warranties of any kind. **STOIC, its operators, employees,
contractors, and affiliates are not liable for any trading losses,
missed opportunities, broker rejections, exchange outages, API outages,
slippage, data feed errors, or any indirect, incidental, special,
consequential, or punitive damages arising from your use of the Service.**

If liability is nevertheless found, total cumulative liability is
limited to the amount you paid us in the **three (3) months** preceding
the event giving rise to the claim.

## 14. Indemnification

You agree to indemnify and hold STOIC harmless from any claim, loss,
liability, or expense (including legal fees) arising from your
violation of these Terms, your trading activity, or your use of the
Service in a manner that harms others.

## 15. Changes to These Terms

We may update these Terms from time to time. We will surface the new
version in-app and bump the version date. Continued use of the Service
after the update constitutes acceptance. Material changes that adversely
affect you will be announced with reasonable notice.

## 16. Governing Law & Disputes

These Terms are governed by the laws of the operator's home jurisdiction.
Disputes shall be resolved by binding individual arbitration; **class
actions are waived** to the maximum extent permitted by law.

## 17. Contact

For all questions, abuse reports, appeals, or DMCA notices, contact us
via the in-app Support channel.

---

*By clicking "I Accept", subscribing, or continuing to use STOIC after
the effective date of these Terms, you confirm you have read, understood,
and agreed to be bound by them.*
""".format(version=TERMS_VERSION, last_updated=LAST_UPDATED)


def get_terms() -> dict:
    return {
        "version": TERMS_VERSION,
        "last_updated": LAST_UPDATED,
        "markdown": TERMS_MARKDOWN,
    }
