# Easy MT5 Connect — spike: WebRequest allow-list without the Options dialog

**Question (spec §B, Phase 2 spike):** can the STOIC installer / VPS Agent make MT5 trust the STOIC
host for `WebRequest` so the user never opens *Tools → Options → Expert Advisors*?

## Desk research (Oct 2026)

| Source | Finding |
|---|---|
| MetaTrader 5 help — *Settings → Expert Advisors*; MQL5 docs `WebRequest` | The allowed-URL list is an interactive terminal setting; the documented startup-config sections (`[Common]`, `[Charts]`, `[Experts]`, `[Objects]`, `[Email]`, `[StartUp]`, `[Tester]`) expose **no WebRequest key**. `[Experts]` only has `AllowLiveTrading`, `AllowDllImport`, `Enabled`, `Account`, `Profile`. |
| mql5.com forum 165425 / 228044 | Users who injected `WebRequestUrl=` keys into `[Experts]` report the terminal **ignores** them. |
| stackoverflow 75505964, mql5 article 10430 | The list lives inside the terminal's `config\` files in an **encrypted** form bound to the installation; copying `config\` between terminals works only sometimes (same terminal id / security context). |
| MQL5 book — network chapter | The only programmatic alternative is a DLL (WinInet/WinHTTP) with `AllowDllImport=1` — rejected for STOIC (DLL imports are a security downgrade and the spec keeps `AllowDllImport=0`). |

**Expected outcome:** attempt A (ini keys) fails; attempt B (golden `config\` copy into a *portable*
per-account terminal that the Agent itself created) is the only candidate and is **unverified** —
it is exactly the Phase-2 model (Agent clones a golden portable folder per account), so it is worth the
empirical run.

## Empirical run (on the VPS, ~5 minutes)

```powershell
cd C:\STOIC   # anywhere
irm https://www.stoicaibot.com/api/setup/installer.ps1 | Out-Null   # (installer already run — EA + stoic.set + stoic-start.ini present)
.\spike_webrequest_startup.ps1 -DataFolder "<MT5 data folder>" -ServerUrl "https://www.stoicaibot.com" `
    -GoldenConfig "<data folder of the terminal where you allowed the URL by hand>\config"
```

`scripts/spike_webrequest_startup.ps1` restarts the terminal twice with alternative configs and reads the
Experts log: **PASS** = EA started and no `WebRequest error 4014` within 75 s.

| Attempt | PASS means | If FAIL |
|---|---|---|
| A — startup ini `[Experts]`/`[WebRequest]` keys | MetaQuotes honours an undocumented key → Phase 2 writes it | expected; move on |
| B — copy golden `config\common.ini` + `terminal.ini` while stopped | Agent can seed every new portable terminal from one golden folder prepared once per VPS → **zero manual steps** | the allow-list stays the one manual step (Install Progress + owner alert already say exactly what to click) |

Record the two PASS/FAIL lines in this file under *Result* and the Phase-2 design follows from them.

## Result

_pending — run on the operator's VPS (fxut10082654)_

## Decision rule for Phase 2

* B PASS → Agent flow: first terminal on a VPS asks the user to allow the URL once (guided, with the
  self-check flag confirming), then snapshots `config\` as the VPS "golden" folder; every further
  account terminal is cloned from it → no manual steps for accounts 2..n.
* B FAIL → keep the single manual step; the Agent still removes MetaEditor, inputs, token and chart
  attachment, and the `webrequest_ok` heartbeat flag drives a one-line banner until the user clicks it.
