//+------------------------------------------------------------------+
//|                                  EmergentTradingBridge.mq5       |
//|              Polls STOIC AI Trading Bot server for trades.       |
//|                                                                  |
//| HOW TO USE:                                                      |
//| 1. Copy this file to: <MT5 Data Folder>/MQL5/Experts/             |
//| 2. In MT5: Tools > Options > Expert Advisors                     |
//|       - Tick: "Allow WebRequest for listed URL"                  |
//|       - Add your server URL (e.g. https://stoic-trading-bot.preview.emergentagent.com)
//| 3. Compile in MetaEditor (F7) and attach to ANY chart            |
//| 4. Inputs:                                                       |
//|       ServerUrl   = auto (installer writes MQL5\Files\STOIC-Server.txt)   |
//|       BridgeToken = (paste from the dashboard > Accounts)         |
//|       PollSeconds = 5                                             |
//|                                                                  |
//| v1.10 — Adds Profit Protection: break-even SL, trailing SL,      |
//|         partial-close at TP1.                                     |
//| v1.21 — Heartbeat now reports current symbol spread (pips) for   |
//|         server-side spread filter.                               |
//| v1.23 — OnTradeTransaction handler reports EVERY deal — including |
//|         manual closes/opens done directly on MT5 — back to STOIC  |
//|         via /api/bridge/external-deal. Closes the "I closed it on |
//|         MT5 but STOIC still shows it open" gap.                   |
//| v1.24 — Heartbeat now reports the broker's MT5 account login +    |
//|         currency so STOIC can detect wrong-terminal misconfigs    |
//|         (e.g. two EAs attached to the same MT5 instance reading   |
//|         the same balance).                                        |
//| v1.25 — Heartbeat now carries a full snapshot of every open       |
//|         position (ticket / symbol / type / volume / price /       |
//|         SL / TP / time / magic / profit). STOIC auto-creates      |
//|         trade records for positions it doesn't yet track —        |
//|         eliminates "I see 4 trades on MT5 but 0 in the bot".      |
//| v1.26 — Autonomous Deal-History Sweep. Every HistorySweepSeconds  |
//|         the EA scans MT5's deal log for the last 24h and pushes   |
//|         any deals not yet reported via /api/bridge/external-deal. |
//|         Catches closes that OnTradeTransaction missed (other      |
//|         terminal, network blip, EA reload) so STOIC stays fully   |
//|         autopilot — no manual "Backfill Exit" needed ever again.  |
//| v1.27 — Position snapshot now carries current_price                |
//|         (PositionGetDouble(POSITION_PRICE_CURRENT)) so STOIC's UI  |
//|         can show broker-real-time prices + unrealised P&L on the   |
//|         Trades page, refreshed every PollSeconds (3-5s) instead    |
//|         of relying on a stale 60-120s external quote cache.        |
//| v1.28 — Auto-selects broker-supported filling mode per symbol.     |
//|         Previously hardcoded ORDER_FILLING_IOC, which made VT      |
//|         Markets (and other FOK-only brokers) return retcode 10013  |
//|         (INVALID_REQUEST) on every trade. Now queries              |
//|         SYMBOL_FILLING_MODE and picks FOK > IOC > RETURN whichever |
//|         the broker accepts.                                        |
//| v1.29 — Symbol-suffix auto-detection + SymbolSelect.               |
//|         Brokers like VT Markets / IC Markets / FXOpen rename       |
//|         instruments with a suffix (XAUUSD.x, XAUUSD.raw,           |
//|         XAUUSDpro, etc.). Before OrderSend, we now call            |
//|         ResolveBrokerSymbol() which tries the bare symbol then     |
//|         every common suffix, picking the first one with a valid    |
//|         live tick. If nothing matches we abort with a clearly      |
//|         tagged "symbol_not_found" error so STOIC's UI can guide    |
//|         the user instead of looping retcode 10013 forever.         |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| v1.40 — FULL_CLOSE modification support + true-slippage report.   |
//|         (1) The EA now consumes FULL_CLOSE entries from the       |
//|         modification queue (slippage veto / auto-deleverage /     |
//|         reconciler force-closes) — previous builds silently       |
//|         ignored them, leaving "stuck modification" warnings and   |
//|         positions the server believed were being closed.          |
//|         (2) /bridge/report now carries requested_price (the price |
//|         at OrderSend) so STOIC measures TRUE broker slippage      |
//|         instead of signal-to-fill latency drift.                  |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| v1.39 — On-demand Deep Broker Sync. The server can now request a  |
//|         full re-scan of the MT5 deal history (default 7 days) via |
//|         the poll-trades response ("sync_request"). The EA pushes  |
//|         EVERY deal in the window to /api/bridge/external-deal     |
//|         (server is idempotent + repairs trades with estimated or  |
//|         missing P&L) and confirms via /api/bridge/sync-complete.  |
//|         Triggered by the dashboard "SYNC" button or automatically |
//|         by the server's auto-heal when it detects inexact records.|
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| v1.38 — Fix retcode 10016 (TRADE_RETCODE_INVALID_STOPS) on        |
//|         brokers with a non-zero SYMBOL_TRADE_STOPS_LEVEL (e.g.    |
//|         Tauro Markets XAUUSD.fx). The EA now clamps SL/TP to the  |
//|         broker's minimum stop distance (stops level, freeze       |
//|         level and live spread, whichever is larger) before every  |
//|         OrderSend, normalises with the TRADED symbol's digits     |
//|         (was chart-symbol _Digits), and on a 10016 rejection      |
//|         retries once with a doubled safety buffer. SL moves       |
//|         (trailing / breakeven) are clamped the same way.          |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| v1.34 — Auto broker-suffix detection. EA enumerates MarketWatch  |
//|         symbols matching common bases (XAU/BTC/forex) and sends  |
//|         the list in `available_symbols` on heartbeat. Backend    |
//|         infers the broker's naming convention so user never      |
//|         needs to manually configure symbol_suffix per broker.    |
//| v1.35 — Faster suffix-discovery on fresh accounts: emit the      |
//|         MarketWatch symbol inventory on every heartbeat for the  |
//|         first 600s after EA attach, then throttle to once/hour.  |
//|         Fixes the "added a new broker, EA emitted symbols once   |
//|         in a noisy moment, backend learned the wrong suffix and  |
//|         then had to wait an hour for the next sample" bug.       |
//| v1.36 — Auto-read bridge token from MQL5\Files\STOIC-Token.txt   |
//|         when the inputs field is empty/placeholder. Pairs with   |
//|         the PowerShell STOIC-Installer.ps1: user runs one shell  |
//|         command on their VPS, installer deploys EA + writes      |
//|         token to the file, EA picks it up on attach. Zero manual |
//|         paste required. (Manual paste still works as override.)  |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| v1.33 — Added .e/.E suffix variants (OnEquity ECN accounts).      |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| v1.32 — Added .fx/.FX/.Fx suffix variants (Tauro Markets /        |
//|         JMFinancial-Server demo accounts).                         |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| v1.31 — Expanded broker symbol-suffix probe list from 17 → 30.    |
//|         Adds .c/.cent (Tauro/JMFinancial cent accounts), .s/.std  |
//|         (FBS/Roboforex standard), .i (IC Markets institutional),  |
//|         ~/.spot (Vantage), and underscore-style variants.          |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| v1.30 — Fix: dashboard EA version was permanently stuck at 1.28  |
//|         because the heartbeat JSON hardcoded "1.28" while        |
//|         #property version said "1.29". Now driven by a single    |
//|         EA_CLIENT_VERSION macro so the two can never drift.       |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| v1.42 — M15 candle feed. Streams the chart symbol's last 96 M15   |
//|         bars to STOIC every CandlesSeconds (300s) so the Market   |
//|         Structure agent can detect break-of-structure, liquidity  |
//|         sweeps, fair value gaps and accumulation/distribution.    |
//+------------------------------------------------------------------+
//| v1.41 — End-of-Day quiet window. Spreads widen drastically across |
//|         all liquidity providers in the final minutes before the   |
//|         daily close. From EodQuietStart (23:40) to EodQuietEnd    |
//|         (00:05) BROKER server time the EA skips poll-trades       |
//|         entirely and every order function (open/modify/close)     |
//|         refuses to fire — preventing severe slippage on automated |
//|         orders. The server re-dispatches queued work after the    |
//|         window (dispatch locks auto-expire). Heartbeats and       |
//|         history sync continue (data-only, no orders).             |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| v1.43 — Depth of Market feed. Subscribes to the broker's order    |
//| v1.44 — Scalp tick stream. Millisecond timer batches EURUSD       |
//|         bid/ask ticks to /api/bridge/ticks for the fast path.     |
//| v1.45 — Deal reports carry position_volume: the broker's          |
//|         REMAINING position volume after each deal, so the server  |
//|         discriminates partial vs full closes from broker state    |
//|         instead of lot arithmetic.                                |
//| v1.46 — Tick-stream hardening. TickStreamSymbol auto-resolves     |
//|         broker suffixes (EURUSD → EURUSD#, EURUSD.r, ...) and     |
//|         SendTicks prints throttled diagnostics to the Experts     |
//|         tab instead of failing silently.                          |
//| v1.47 — Multi-symbol candle feed. SendCandles streams M15 bars    |
//|         for the chart symbol AND every TrackedSymbols entry       |
//|         (broker-suffix resolved), so moving the EA to another     |
//|         chart no longer silently starves other pairs of data.     |
//|         book (MarketBookAdd) and streams bid/ask depth to STOIC   |
//|         every DomSeconds (30s) for the Liquidity Mapping agent    |
//|         (resting liquidity, walls, book imbalance). Degrades      |
//|         gracefully: if the broker provides no DOM for the symbol  |
//|         (common on CFD feeds) nothing is sent and the agent maps  |
//|         liquidity from candles alone.                             |
//| v1.48 — Heartbeat reports per-symbol broker stop constraints      |
//|         (SYMBOL_TRADE_STOPS_LEVEL / FREEZE_LEVEL in points +      |
//|         point size) for the chart symbol, TrackedSymbols and all  |
//|         open-position symbols, so the backend can respect precise |
//|         minimum stop distances when placing emergency stops.      |
//| v1.49 — Symbol specs additionally report SYMBOL_TRADE_MODE so the |
//|         backend can refuse orders the broker would reject         |
//|         (disabled / long-only / short-only / close-only symbols). |
//| v1.50 — Execution-integrity hardening (audit r4):                 |
//|         (a) Command fence: modifications carry intent_id + seq;   |
//|             executed intents persist in Global Variables so a     |
//|             duplicate/older command is skipped (with a replay     |
//|             re-ack) and every ack echoes its intent_id.           |
//|         (b) Durable new-order intent journal: RECEIVED →          |
//|             ORDER_SENT → BROKER_TICKET_ASSIGNED → ACK_SENT per    |
//|             trade_id; a redispatched trade_id re-reports the      |
//|             previous result and can NEVER OrderSend twice.        |
//|             Orders carry the trade_id as position comment for     |
//|             crash-window recovery.                                |
//|         (c) Actual-SL reporting: open reports + MODIFY_SL acks    |
//|             return requested_sl / applied_sl (post-clamp) /       |
//|             confirmed_position_sl (live POSITION_SL).             |
//|         (d) Partial-close acks report the ACTUAL remaining broker |
//|             volume, not just the intended one.                    |
//|         (e) Broker-native OrderCheck() preflight before every     |
//|             OrderSend — margin / volume limits / stop levels /    |
//|             fill policy validated by MT5; failures report a       |
//|             structured preflight_failed rejection. The retry      |
//|             pass re-runs OrderCheck under the same guarantees.    |
//|         (f) Exact-once acks: intents are consumed ONLY on broker- |
//|             confirmed success (or explicit terminal states);      |
//|             replays return the JOURNALED outcome + live facts;    |
//|             stale sequences ack superseded; missing positions     |
//|             ack terminal/retryable from broker history.           |
//|         (g) close_requested path fenced: deterministic close      |
//|             intent, result journal, and 'closed' only reported    |
//|             when the broker position is VERIFIED absent.          |
//| v1.51 — OrderCheck preflight COMPLETED on every OrderSend path:   |
//|         (a) ApplyFullClose / ClosePosition / ApplyModifySL /      |
//|             ApplyPartialClose now run the same broker-native      |
//|             OrderCheck() preflight as ExecuteTrade (shared        |
//|             PreflightOk helper) — margin, volume limits,          |
//|             stop/freeze levels and fill policy validated by MT5   |
//|             itself before any request reaches the broker.         |
//|             Failed preflights ack structured preflight_failed     |
//|             errors (retryable) without consuming intents.         |
//|         (b) ExecuteTrade normalises the lot against the BROKER's  |
//|             SYMBOL_VOLUME_MIN / MAX / STEP before preflight — a   |
//|             lot below the broker minimum reports a terminal       |
//|             volume_below_broker_min failure instead of a raw      |
//|             broker rejection.                                     |
//| v1.52 — Execution-truth audit (P0):                               |
//|         (a) Partial fills on NEW orders: DONE_PARTIAL is a REAL   |
//|             position — the open flow now verifies the actual      |
//|             broker deal + resulting position for both full and    |
//|             partial fills and reports filled_volume/partial_fill. |
//|             A failed retcode with a live position (requote edge)  |
//|             is recovered, never reported failed.                  |
//|         (b) Order ticket != position ticket: order_ticket,        |
//|             deal_ticket and position identifier are resolved      |
//|             (DEAL_POSITION_ID), stored separately and reported.   |
//|             mt5_ticket now carries the POSITION identifier.       |
//|         (c) Netting-aware recovery: crash-window replay searches  |
//|             order/deal history by magic + trade_id comment +      |
//|             symbol + side + volume + execution window (comment    |
//|             scan alone misses netting merges).                    |
//|         (d) Exact 64-bit ticket journaling: tickets split into    |
//|             two 32-bit halves (GV doubles are exact <= 2^53 but   |
//|             split storage removes the risk class entirely).       |
//|         (e) MODIFY_SL: 'request accepted' and 'stop confirmed'    |
//|             are separate states — the intent is consumed ONLY     |
//|             after the live position shows the stop.               |
//|         (f) PARTIAL_CLOSE success is judged from the ACTUAL       |
//|             remaining volume within the broker volume step, not   |
//|             from the retcode.                                     |
//| v1.53 — Netting truth + unresolved-accept lifecycle (P0):          |
//|         (a) An accepted order WITHOUT a resolved position          |
//|             identifier is never reported open: it acks             |
//|             'accepted_unresolved' (trade stays pending, risk       |
//|             reservation stays active) and journals JR_ACCEPTED;    |
//|             redispatch re-runs resolution, never resends.          |
//|         (b) filled_volume for the trade = Σ DEAL_VOLUME of the     |
//|             deals belonging to OUR order (OrderFilledVolume) —     |
//|             POSITION_VOLUME on netting is the whole symbol         |
//|             position and is reported separately as                 |
//|             position_volume (broker exposure, not attribution).    |
//|         (c) partial_fill truth = deal volume vs requested within   |
//|             half a volume step; DONE_PARTIAL is diagnostic only.  |
//| v1.54 — Broker certification completeness (correction #5):        |
//|         (a) symbol_specs now include tick_size, tick_value,       |
//|             contract_size and volume min/max/step;                |
//|         (b) heartbeat reports broker_time (server GMT offset,     |
//|             server time and today's trading sessions for the      |
//|             chart symbol) so DST/session handling is certifiable. |
//| v1.60 — N98-6: heartbeat reports ACCOUNT_TRADE_MODE as the broker  |
//|         sees it ("demo" | "real" | "contest"). The server trusts   |
//|         DEMO only when the broker says so: "real" voids any DEMO   |
//|         attestation (LIVE gates apply), "demo" replaces the        |
//|         server-name heuristic / admin override for the proof.     |
//| v1.56 — Candle feed covers the tick-stream symbol: SendCandles    |
//|         now also streams M15 bars for TickStreamSymbol even when  |
//|         it is neither the chart symbol nor in TrackedSymbols      |
//|         (fixes permanent 'insufficient M15 history' when scalping |
//|         EURUSD from a GOLD/other-symbol chart).                   |
//+------------------------------------------------------------------+
#property copyright "STOIC AI Trading"
#property version   "1.61"
#property strict

// Single source of truth for the version string we report to STOIC on every
// heartbeat. Keep this in sync with #property version above. Bumping ONLY
// one of the two causes the dashboard to show a stale EA version even
// though MT5 itself loads the new binary.
#define EA_CLIENT_VERSION "1.61"

input string ServerUrl              = "https://www.stoicaibot.com";  // auto-loaded from MQL5\Files\STOIC-Server.txt (installer) when left at default
input string BridgeToken            = "PASTE_YOUR_BRIDGE_TOKEN_HERE";
input string InstallationId         = "";  // v1.55 — issued by pairing claim; auto-loaded from STOIC-Installation.txt when blank
input string TrackedSymbols         = "XAUUSD,BTCUSD";  // comma list — spreads sent on heartbeat
input int    PollSeconds            = 5;
input int    Slippage               = 10;
input int    MagicNumber            = 901234;
input int    HistorySweepSeconds    = 60;     // how often to scan MT5 deal history
input int    HistoryLookbackSeconds = 86400;  // initial backfill window (24h)
input int    HistorySweepMaxDeals   = 50;     // hard cap per sweep so a fresh chart doesn't flood STOIC

// EA v1.41 · End-of-Day quiet window (broker server time, HH:MM).
// Spreads widen drastically at the daily close — no order operations
// (open / modify / close) are attempted inside the window.
input bool   EodQuietEnabled        = true;
input string EodQuietStart          = "23:40";  // broker server time
input string EodQuietEnd            = "00:05";  // broker server time

// EA v1.42 · M15 candle feed interval for the Market Structure agent.
input int    CandlesSeconds         = 300;

// EA v1.43 · Depth of Market feed for the Liquidity Mapping agent.
input bool   DomEnabled             = true;
input int    DomSeconds             = 30;

// EA v1.44 · Scalp fast path — bid/ask tick stream (EURUSD subsystem).
// When enabled the EA switches to a millisecond timer and batches every
// tick of TickStreamSymbol to /api/bridge/ticks each TickBatchMs.
input bool   TickStreamEnabled      = true;
input string TickStreamSymbol       = "EURUSD";
input int    TickBatchMs            = 1000;

datetime _last_slow_run        = 0;
ulong    _last_tick_msc        = 0;
string   _tick_symbol          = "";       // v1.46: broker-resolved stream symbol
datetime _last_tick_warn       = 0;        // v1.46: throttle diagnostics

// v1.46 — resolve the broker's actual symbol name for the tick stream.
// Accepts a base name (EURUSD) and finds suffixed variants (EURUSD#,
// EURUSD.r, EURUSD+, ...) in the broker's symbol list.
string ResolveTickSymbol(string want) {
   MqlTick probe;
   if (SymbolInfoTick(want, probe)) return want;
   SymbolSelect(want, true);
   if (SymbolInfoTick(want, probe)) return want;
   int total = SymbolsTotal(false);
   for (int i = 0; i < total; i++) {
      string s = SymbolName(i, false);
      if (StringFind(s, want) == 0) {
         SymbolSelect(s, true);
         return s;
      }
   }
   return want;
}

void TickStreamWarn(string msg) {
   if (TimeCurrent() - _last_tick_warn < 60) return;   // 1 line/min max
   _last_tick_warn = TimeCurrent();
   Print("STOIC TickStream: ", msg);
}

datetime _last_candles_sent    = 0;
datetime _last_dom_sent        = 0;
bool     _dom_subscribed       = false;
datetime lastPoll              = 0;
datetime lastHistorySweep      = 0;
datetime lastReportedDealTime  = 0;   // high-watermark — never re-push deals older than this

// EA v1.36 · Resolved bridge token. `input string BridgeToken` is read-only
// post-init in MQL5, so we copy the resolved value (either the user-pasted
// input OR the contents of MQL5\Files\STOIC-Token.txt deployed by the
// STOIC-Installer.ps1) into this mutable global at OnInit() and use it
// everywhere downstream. This closes the loop on the PowerShell installer:
// the user pastes one PowerShell line on their VPS, the installer drops the
// token into the right file, the EA picks it up at attach — zero manual
// paste in the MT5 inputs dialog required.
string EffectiveToken = "";

// EA v1.55 · Resolved installation identity. The server-issued
// installation_id (from the pairing claim) proves WHICH physical
// EA install is talking. Heartbeats without it are treated as
// UNVERIFIED by the server: telemetry only — no execution-lease
// renewal, no live trading.
string EffectiveInstallation = "";
// r25 P1-01: installer-measured SHA-256 of the deployed EX5 (MQL5\Files\STOIC-Proof.txt).
// The EA cannot hash its own binary (Files sandbox), so the installer measures it
// and the server admits the proof only when it matches the hash the installer
// recorded for THIS installation AND the signed release hash.
string EffectiveProofHash = "";

string ResolveProofHash() {
   if (!FileIsExist("STOIC-Proof.txt")) {
      Print("STOIC: no STOIC-Proof.txt in MQL5\\Files - binary proof absent (re-run the STOIC installer). Live activation stays blocked.");
      return "";
   }
   int fh = FileOpen("STOIC-Proof.txt", FILE_READ | FILE_TXT | FILE_ANSI);
   if (fh == INVALID_HANDLE) return "";
   string h = "";
   while (!FileIsEnding(fh)) {
      string line = FileReadString(fh);
      StringTrimLeft(line); StringTrimRight(line);
      if (StringLen(line) == 0 || StringGetCharacter(line, 0) == '#') continue;
      h = line; break;
   }
   FileClose(fh);
   StringToLower(h);
   if (StringLen(h) != 64) { Print("STOIC: STOIC-Proof.txt malformed - ignoring."); return ""; }
   Print("STOIC: binary proof loaded (", StringSubstr(h, 0, 12), "...).");
   return h;
}

//+------------------------------------------------------------------+
// Resolve the installation id from input OR the installer drop file.
string ResolveInstallationId() {
   string input_trim = InstallationId;
   StringTrimLeft(input_trim);
   StringTrimRight(input_trim);
   if (StringLen(input_trim) > 0) {
      Print("STOIC: using installation id from EA inputs dialog.");
      return input_trim;
   }
   if (!FileIsExist("STOIC-Installation.txt")) {
      Print("STOIC: WARNING — no InstallationId in EA inputs AND no STOIC-Installation.txt in MQL5\\Files. Heartbeats will be UNVERIFIED (telemetry only, no live trading). Pair this terminal from the STOIC dashboard.");
      return "";
   }
   int fh = FileOpen("STOIC-Installation.txt", FILE_READ | FILE_TXT | FILE_ANSI);
   if (fh == INVALID_HANDLE) {
      Print("STOIC: WARNING — STOIC-Installation.txt exists but FileOpen failed (", GetLastError(), ").");
      return "";
   }
   string inst = "";
   while (!FileIsEnding(fh)) {
      string line = FileReadString(fh);
      StringTrimLeft(line); StringTrimRight(line);
      if (StringLen(line) == 0) continue;
      if (StringGetCharacter(line, 0) == '#') continue;
      inst = line;
      break;
   }
   FileClose(fh);
   if (StringLen(inst) > 0)
      Print("STOIC: installation id auto-loaded from MQL5\\Files\\STOIC-Installation.txt.");
   return inst;
}

//+------------------------------------------------------------------+
// Resolve the bridge token from input OR the auto-installer drop file.
// Returns "" if neither source has a usable token (user needs to paste).
string ResolveBridgeToken() {
   string input_trim = BridgeToken;
   StringTrimLeft(input_trim);
   StringTrimRight(input_trim);
   bool input_usable = (StringLen(input_trim) > 0
                        && input_trim != "PASTE_YOUR_BRIDGE_TOKEN_HERE");
   if (input_usable) {
      Print("STOIC: using bridge token from EA inputs dialog.");
      return input_trim;
   }
   // Fallback to the file dropped by STOIC-Installer.ps1.
   // MQL5 sandboxes file I/O to MQL5\Files by default.
   if (!FileIsExist("STOIC-Token.txt")) {
      Print("STOIC: WARNING — no token in EA inputs AND no STOIC-Token.txt found in MQL5\\Files. Heartbeats will be rejected. Either paste BridgeToken in inputs, or run the PowerShell auto-installer from the dashboard.");
      return "";
   }
   int fh = FileOpen("STOIC-Token.txt", FILE_READ | FILE_TXT | FILE_ANSI);
   if (fh == INVALID_HANDLE) {
      Print("STOIC: WARNING — STOIC-Token.txt exists but FileOpen failed (", GetLastError(), ").");
      return "";
   }
   string token = "";
   while (!FileIsEnding(fh)) {
      string line = FileReadString(fh);
      StringTrimLeft(line); StringTrimRight(line);
      // Skip blank lines and comment lines written by the installer.
      if (StringLen(line) == 0) continue;
      if (StringGetCharacter(line, 0) == '#') continue;
      token = line;
      break;  // first non-comment, non-blank line is the token
   }
   FileClose(fh);
   if (StringLen(token) == 0) {
      Print("STOIC: WARNING — STOIC-Token.txt is empty or comments-only.");
      return "";
   }
   Print("STOIC: bridge token auto-loaded from MQL5\\Files\\STOIC-Token.txt (length=", StringLen(token), ").");
   return token;
}

//+------------------------------------------------------------------+
// Installer v1.5 — server URL auto-load: STOIC-Installer.ps1 writes MQL5\Files\STOIC-Server.txt
// (the host the pairing token was redeemed at). The file wins while the ServerUrl input is at its
// compiled default; an operator who explicitly changed the input keeps that value.
#define SERVER_URL_DEFAULT "https://www.stoicaibot.com"
string g_server_url = SERVER_URL_DEFAULT;
bool   g_webrequest_ok = true;   // false after a 4014 until a request succeeds (reported in heartbeats)
string ResolveServerUrl() {
   string input_trim = ServerUrl;
   StringTrimLeft(input_trim); StringTrimRight(input_trim);
   if (StringLen(input_trim) > 0 && input_trim != SERVER_URL_DEFAULT) {
      Print("STOIC: using ServerUrl from EA inputs dialog.");
      return input_trim;
   }
   if (!FileIsExist("STOIC-Server.txt")) return (StringLen(input_trim) > 0 ? input_trim : SERVER_URL_DEFAULT);
   int fh = FileOpen("STOIC-Server.txt", FILE_READ | FILE_TXT | FILE_ANSI);
   if (fh == INVALID_HANDLE) return input_trim;
   string url = "";
   while (!FileIsEnding(fh)) {
      string line = FileReadString(fh);
      StringTrimLeft(line); StringTrimRight(line);
      if (StringLen(line) == 0 || StringGetCharacter(line, 0) == '#') continue;
      url = line;
      break;
   }
   FileClose(fh);
   if (StringLen(url) < 12 || StringFind(url, "https://") != 0) {   // audit #10 — bridge token never travels in clear
      Print("STOIC: ignoring STOIC-Server.txt (must start with https://).");
      return (StringLen(input_trim) > 0 ? input_trim : SERVER_URL_DEFAULT);
   }
   while (StringLen(url) > 0 && StringGetCharacter(url, StringLen(url) - 1) == '/') url = StringSubstr(url, 0, StringLen(url) - 1);
   Print("STOIC: server URL auto-loaded from MQL5\\Files\\STOIC-Server.txt: ", url);
   return url;
}

int OnInit() {
   // EA v1.44 — tick streaming needs a sub-second timer; slow tasks below
   // keep their PollSeconds cadence via the _last_slow_run gate in OnTimer.
   if (TickStreamEnabled) {
      _tick_symbol = ResolveTickSymbol(TickStreamSymbol);
      SymbolSelect(_tick_symbol, true);
      if (StringCompare(_tick_symbol, TickStreamSymbol) != 0)
         Print("STOIC TickStream: resolved '", TickStreamSymbol,
               "' to broker symbol '", _tick_symbol, "'");
      Print("STOIC TickStream: ENABLED for ", _tick_symbol,
            " (batch ", MathMax(250, TickBatchMs), "ms)");
      EventSetMillisecondTimer(MathMax(250, TickBatchMs));
   } else {
      EventSetTimer(PollSeconds);
   }
   // First sweep covers HistoryLookbackSeconds backwards so any ghosts
   // (closed on another terminal while EA was offline) get backfilled
   // automatically once the user installs v1.26.
   lastReportedDealTime = TimeCurrent() - HistoryLookbackSeconds;
   // EA v1.35: record boot time so the SendHeartbeat() symbol-emit throttle
   // can stream the MarketWatch inventory aggressively for the first 10 min
   // (suffix discovery converges quickly on a freshly attached account).
   _ea_boot_time = TimeCurrent();
   // Installer v1.5: server URL from inputs OR the installer drop file.
   g_server_url = ResolveServerUrl();
   // EA v1.36: resolve token from inputs OR auto-installer drop file.
   EffectiveToken = ResolveBridgeToken();
   // EA v1.55: resolve the installation identity for verified heartbeats.
   EffectiveInstallation = ResolveInstallationId();
   EffectiveProofHash = ResolveProofHash();
   // EA v1.43: subscribe to the broker's order book (no-op if unsupported).
   if (DomEnabled) _dom_subscribed = MarketBookAdd(_Symbol);
   // EA v1.50: prune intent-journal Global Variables idle for 7+ days.
   SweepJournal();
   Print("STOIC Bridge EA v", EA_CLIENT_VERSION, " started. Polling: ", g_server_url);
   // EA v1.61 self-check — the same facts go out as heartbeat flags so the dashboard can say what to fix.
   if (!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) Print("STOIC self-check: AutoTrading is OFF — click the AutoTrading button (top toolbar) so it turns green.");
   if (!MQLInfoInteger(MQL_TRADE_ALLOWED))           Print("STOIC self-check: 'Allow Algo Trading' is unticked for this EA — chart → EA properties → Common tab.");
   Print("STOIC self-check: account mode = ", TradeModeString(), ", trade allowed = ", (AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) ? "yes" : "no"));
   SendHeartbeat();
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) {
   EventKillTimer();
   if (_dom_subscribed) MarketBookRelease(_Symbol);
}

//+------------------------------------------------------------------+
//| EA v1.41 — End-of-Day quiet window helpers.                       |
//+------------------------------------------------------------------+
// Parse "HH:MM" → minutes since midnight. Falls back on invalid input.
int ParseHHMM(string s, int fallback_min) {
   int c = StringFind(s, ":");
   if (c <= 0) return fallback_min;
   int h = (int)StringToInteger(StringSubstr(s, 0, c));
   int m = (int)StringToInteger(StringSubstr(s, c + 1));
   if (h < 0 || h > 23 || m < 0 || m > 59) return fallback_min;
   return h * 60 + m;
}

datetime _last_quiet_log = 0;

// True while inside [EodQuietStart, EodQuietEnd) in BROKER server time.
// Handles windows that wrap midnight (23:40 → 00:05).
bool IsEodQuietWindow() {
   if (!EodQuietEnabled) return false;
   MqlDateTime bt;
   TimeToStruct(TimeCurrent(), bt);   // TimeCurrent() is broker server time
   int now_min   = bt.hour * 60 + bt.min;
   int start_min = ParseHHMM(EodQuietStart, 23 * 60 + 40);
   int end_min   = ParseHHMM(EodQuietEnd, 5);
   bool quiet;
   if (start_min <= end_min) quiet = (now_min >= start_min && now_min < end_min);
   else                      quiet = (now_min >= start_min || now_min < end_min);
   if (quiet && TimeCurrent() - _last_quiet_log >= 60) {
      Print("STOIC: EOD quiet window (", EodQuietStart, "-", EodQuietEnd,
            " broker time) — order operations paused, spreads widen at the daily close.");
      _last_quiet_log = TimeCurrent();
   }
   return quiet;
}

//+------------------------------------------------------------------+
//| v1.50 — Durable intent journal + command fence.                   |
//| Terminal Global Variables persist across EA/terminal restarts:    |
//|   STOIC.I.<intent_id>  executed command instances (fence)         |
//|   STOIC.S.<trade_id>   newest executed command seq per trade      |
//|   STOIC.T.<trade_id>   new-order journal state                    |
//|   STOIC.K.<trade_id>   broker ticket  ·  STOIC.P.<trade_id> fill  |
//| Journal states: RECEIVED → ORDER_SENT → BROKER_TICKET_ASSIGNED →  |
//| ACK_SENT (or FAILED). A redispatched trade_id re-reports the      |
//| previous result — it can NEVER submit a second broker order.      |
//+------------------------------------------------------------------+
#define JR_RECEIVED      1
#define JR_ORDER_SENT    2
#define JR_TICKET        3
#define JR_ACK_SENT      4
#define JR_FAILED        5
#define JR_ACCEPTED      6   // v1.53 — broker accepted, position unresolved

string JKey(string kind, string id) { return "STOIC." + kind + "." + id; }

double JGet(string kind, string id) {
   string k = JKey(kind, id);
   return GlobalVariableCheck(k) ? GlobalVariableGet(k) : 0;
}

void JSet(string kind, string id, double v) { GlobalVariableSet(JKey(kind, id), v); }

bool IntentDone(string intent) {
   return StringLen(intent) > 0 && GlobalVariableCheck(JKey("I", intent));
}

void MarkIntentDone(string intent, long seq, string trade_id,
                    double outcome = 1) {
   // outcome journal: 1 = broker-confirmed success, 2 = terminal failure.
   // Failed-but-retryable commands are NEVER marked — they stay retryable.
   if (StringLen(intent) > 0)
      GlobalVariableSet(JKey("I", intent), outcome);
   if (seq > 0 && seq > (long)JGet("S", trade_id))
      JSet("S", trade_id, (double)seq);
}

// v1.52 — EXACT 64-bit ticket journaling. Global Variables store doubles,
// so tickets are split into two 32-bit halves (STOIC.<kind>H / <kind>L).
// Legacy single-double entries from pre-1.52 journals keep reading.
void JSetTicket(string kind, string id, ulong ticket) {
   JSet(kind + "H", id, (double)(ticket >> 32));
   JSet(kind + "L", id, (double)(ticket & 0xFFFFFFFF));
}

ulong JGetTicket(string kind, string id) {
   ulong hi = (ulong)JGet(kind + "H", id);
   ulong lo = (ulong)JGet(kind + "L", id);
   if (hi > 0 || lo > 0) return (hi << 32) | lo;
   double legacy = JGet(kind, id);
   return (legacy > 0 ? (ulong)legacy : 0);
}

// v1.52 — authoritative deal → position link. An MT5 ORDER ticket is not
// the POSITION ticket (netting merges fills); DEAL_POSITION_ID is truth.
ulong PositionIdFromDeal(ulong deal_ticket) {
   if (deal_ticket == 0) return 0;
   if (!HistoryDealSelect(deal_ticket)) {
      HistorySelect(TimeCurrent() - 86400, TimeCurrent() + 60);
      if (!HistoryDealSelect(deal_ticket)) return 0;
   }
   return (ulong)HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID);
}

// v1.53 — a trade's TRUE fill = sum of DEAL_VOLUME over the deals that
// belong to OUR order. POSITION_VOLUME on a netting account is the whole
// symbol position (pre-existing lots included) and must never be
// attributed to this trade.
double OrderFilledVolume(ulong order_ticket) {
   if (order_ticket == 0) return 0;
   double total = 0;
   if (HistorySelect(TimeCurrent() - 86400, TimeCurrent() + 60)) {
      for (int i = HistoryDealsTotal() - 1; i >= 0; i--) {
         ulong dtk = HistoryDealGetTicket(i);
         if (dtk == 0) continue;
         if ((ulong)HistoryDealGetInteger(dtk, DEAL_ORDER) != order_ticket)
            continue;
         total += HistoryDealGetDouble(dtk, DEAL_VOLUME);
      }
   }
   return total;
}

// v1.52 — select the live position for a position IDENTIFIER. Hedging:
// ticket == identifier. Netting: fills merge into one symbol position —
// scan identifiers, then fall back to the symbol's netted position.
bool SelectPositionById(ulong position_id, string symbol) {
   if (position_id > 0 && PositionSelectByTicket(position_id)) return true;
   for (int i = PositionsTotal() - 1; i >= 0; i--) {
      ulong tk = PositionGetTicket(i);
      if (tk == 0) continue;
      if ((ulong)PositionGetInteger(POSITION_IDENTIFIER) == position_id)
         return true;
   }
   bool netting = ((ENUM_ACCOUNT_MARGIN_MODE)
                   AccountInfoInteger(ACCOUNT_MARGIN_MODE)
                   != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING);
   if (netting && symbol != "" && PositionSelect(symbol)) return true;
   return false;
}

// Drop journal entries idle for 7+ days (MT5 auto-expires GVs at 4 weeks).
void SweepJournal() {
   datetime cutoff = TimeCurrent() - 7 * 86400;
   for (int i = GlobalVariablesTotal() - 1; i >= 0; i--) {
      string name = GlobalVariableName(i);
      if (StringFind(name, "STOIC.") != 0) continue;
      if (GlobalVariableTime(name) < cutoff) GlobalVariableDel(name);
   }
}

// Crash-window recovery: orders carry trade_id as the position comment.
long FindPositionByComment(string trade_id) {
   for (int i = PositionsTotal() - 1; i >= 0; i--) {
      ulong tk = PositionGetTicket(i);
      if (tk == 0) continue;
      if (PositionGetString(POSITION_COMMENT) == trade_id) return (long)tk;
   }
   return 0;
}

// v1.52 — netting-aware trade → position resolution. Comment scan first
// (hedging fast path), then order/deal HISTORY by magic + trade_id comment
// + symbol + side + volume inside the execution window; returns the deal's
// POSITION_ID (0 if unresolvable).
ulong FindPositionForTrade(string trade_id, string symbol, string action,
                           double lot) {
   long by_comment = FindPositionByComment(trade_id);
   if (by_comment > 0 && PositionSelectByTicket((ulong)by_comment))
      return (ulong)PositionGetInteger(POSITION_IDENTIFIER);
   if (!HistorySelect(TimeCurrent() - 7200, TimeCurrent() + 60)) return 0;
   long want_type = (action == "BUY") ? DEAL_TYPE_BUY : DEAL_TYPE_SELL;
   for (int i = HistoryDealsTotal() - 1; i >= 0; i--) {
      ulong dtk = HistoryDealGetTicket(i);
      if (dtk == 0) continue;
      if (HistoryDealGetInteger(dtk, DEAL_MAGIC) != MagicNumber) continue;
      long entry = HistoryDealGetInteger(dtk, DEAL_ENTRY);
      if (entry != DEAL_ENTRY_IN && entry != DEAL_ENTRY_INOUT) continue;
      bool comment_hit = (StringFind(
         HistoryDealGetString(dtk, DEAL_COMMENT), trade_id) >= 0);
      bool profile_hit = (symbol != ""
         && HistoryDealGetString(dtk, DEAL_SYMBOL) == symbol
         && HistoryDealGetInteger(dtk, DEAL_TYPE) == want_type
         && MathAbs(HistoryDealGetDouble(dtk, DEAL_VOLUME) - lot) < 1e-8);
      if (comment_hit || profile_hit)
         return (ulong)HistoryDealGetInteger(dtk, DEAL_POSITION_ID);
   }
   return 0;
}

// v1.50 — was this position genuinely closed (an OUT deal exists)?
bool PositionClosedInHistory(long ticket) {
   if (!HistorySelectByPosition((ulong)ticket)) return false;
   for (int i = HistoryDealsTotal() - 1; i >= 0; i--) {
      ulong d = HistoryDealGetTicket(i);
      if (d == 0) continue;
      if ((ENUM_DEAL_ENTRY)HistoryDealGetInteger(d, DEAL_ENTRY) == DEAL_ENTRY_OUT)
         return true;
   }
   return false;
}

// v1.50 — missing-position ack: TERMINAL when broker history proves the
// position closed (server clears the command), RETRYABLE otherwise (a
// temporary sync gap — server keeps the command pending).
void AckMissingPosition(string trade_id, string mod_type, long ticket,
                        string intent) {
   bool closed = PositionClosedInHistory(ticket);
   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"%s\",\"success\":false,%s\"intent_id\":\"%s\",\"error\":\"%s\"}",
      EffectiveToken, trade_id, mod_type,
      (closed ? "\"terminal\":true," : "\"retryable\":true,"),
      intent,
      (closed ? "position_not_found" : "position_temporarily_unavailable"));
   HttpPost(g_server_url + "/api/bridge/modification-ack", body);
   if (closed) MarkIntentDone(intent, 0, trade_id, 2);   // terminal — never retried
}

void OnTimer() {
   // EA v1.44 — fast lane: stream ticks every timer fire when enabled.
   if (TickStreamEnabled) SendTicks();
   // Slow lane: heartbeat / polls / feeds keep their PollSeconds cadence.
   if (TimeCurrent() - _last_slow_run < PollSeconds) return;
   _last_slow_run = TimeCurrent();
   SendHeartbeat();
   PollPendingTrades();
   // EA v1.42 — M15 candle feed for the Market Structure agent.
   if (TimeCurrent() - _last_candles_sent >= CandlesSeconds) {
      SendCandles();
      _last_candles_sent = TimeCurrent();
   }
   // EA v1.43 — Depth of Market feed for the Liquidity Mapping agent.
   if (_dom_subscribed && TimeCurrent() - _last_dom_sent >= DomSeconds) {
      SendDom();
      _last_dom_sent = TimeCurrent();
   }
   // Autonomous history sweep — at most once every HistorySweepSeconds so
   // we don't bombard the server with redundant /external-deal calls.
   if (TimeCurrent() - lastHistorySweep >= HistorySweepSeconds) {
      SweepDealHistory();
      lastHistorySweep = TimeCurrent();
   }
}

//+------------------------------------------------------------------+
//| EA v1.42 — stream M15 candles to STOIC so the Market Structure    |
//| agent can detect BOS / liquidity sweeps / FVGs.                   |
//| EA v1.47 — sends the chart symbol AND every TrackedSymbols entry  |
//| (broker-suffix resolved) so all tracked pairs keep fresh data     |
//| regardless of which chart the EA is attached to.                  |
//+------------------------------------------------------------------+
void SendCandlesFor(string sym) {
   MqlRates rates[];
   int n = CopyRates(sym, PERIOD_M15, 0, 96, rates);
   if (n < 10) return;
   string bars = "[";
   for (int i = 0; i < n; i++) {
      if (i > 0) bars += ",";
      bars += StringFormat(
         "{\"t\":%I64d,\"o\":%.5f,\"h\":%.5f,\"l\":%.5f,\"c\":%.5f,\"v\":%I64d}",
         (long)rates[i].time, rates[i].open, rates[i].high, rates[i].low,
         rates[i].close, rates[i].tick_volume);
   }
   bars += "]";
   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"symbol\":\"%s\",\"timeframe\":\"M15\",\"bars\":%s}",
      EffectiveToken, sym, bars);
   HttpPost(g_server_url + "/api/bridge/candles", body);
}

void SendCandles() {
   string sent = "," + _Symbol + ",";
   SendCandlesFor(_Symbol);
   // v1.56 — the scalp engine needs M15 history for the TICK STREAM symbol;
   // stream its candles even when it is neither the chart symbol nor listed
   // in TrackedSymbols (previously EURUSD scalp on a GOLD chart never
   // received candles → permanent "insufficient M15 history").
   if (TickStreamEnabled) {
      string ts = (StringLen(_tick_symbol) > 0)
                  ? _tick_symbol : ResolveTickSymbol(TickStreamSymbol);
      if (StringFind(sent, "," + ts + ",") < 0) {
         sent += ts + ",";
         SendCandlesFor(ts);
      }
   }
   string list = TrackedSymbols;
   StringReplace(list, " ", "");
   string parts[];
   int k = StringSplit(list, ',', parts);
   for (int i = 0; i < k; i++) {
      if (StringLen(parts[i]) == 0) continue;
      string resolved = ResolveTickSymbol(parts[i]);
      if (StringFind(sent, "," + resolved + ",") >= 0) continue;
      sent += resolved + ",";
      SendCandlesFor(resolved);
   }
}

//+------------------------------------------------------------------+
//| EA v1.43 — stream the broker's live order book (Depth of Market)  |
//| to STOIC. The Liquidity Mapping agent uses it to detect bid/ask   |
//| walls and book imbalance. Silently no-ops when the broker gives   |
//| no DOM for the symbol.                                            |
//+------------------------------------------------------------------+
void SendDom() {
   MqlBookInfo book[];
   if (!MarketBookGet(_Symbol, book)) return;
   int n = ArraySize(book);
   if (n < 2) return;
   string bids = "", asks = "";
   int nb = 0, na = 0;
   for (int i = 0; i < n; i++) {
      double vol = (book[i].volume_real > 0) ? book[i].volume_real
                                             : (double)book[i].volume;
      string row = StringFormat("{\"p\":%.5f,\"v\":%.2f}", book[i].price, vol);
      if ((book[i].type == BOOK_TYPE_BUY || book[i].type == BOOK_TYPE_BUY_MARKET) && nb < 16) {
         if (nb > 0) bids += ",";
         bids += row; nb++;
      } else if ((book[i].type == BOOK_TYPE_SELL || book[i].type == BOOK_TYPE_SELL_MARKET) && na < 16) {
         if (na > 0) asks += ",";
         asks += row; na++;
      }
   }
   if (nb == 0 && na == 0) return;
   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"symbol\":\"%s\",\"bids\":[%s],\"asks\":[%s]}",
      EffectiveToken, _Symbol, bids, asks);
   HttpPost(g_server_url + "/api/bridge/dom", body);
}

//+------------------------------------------------------------------+
//| OnTradeTransaction — fires on EVERY broker trade event.          |
//| Captures manual MT5-side opens/closes (bypassing OrderSend from   |
//| this EA) so STOIC stays in sync with the broker even when the     |
//| user clicks Buy/Sell/Close directly in the MT5 terminal.          |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction& trans,
                        const MqlTradeRequest&    request,
                        const MqlTradeResult&     result) {
   // Only act on DEAL_ADD events — they fire once per executed deal
   // (open, close or partial-close). Other transaction types (order
   // updates, history pruning) would generate duplicates or noise.
   if (trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   ulong deal_id = trans.deal;
   if (deal_id == 0) return;
   if (!HistoryDealSelect(deal_id)) return;

   long   position_id = (long)HistoryDealGetInteger(deal_id, DEAL_POSITION_ID);
   if (position_id == 0) return;   // balance ops, deposits etc.
   ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal_id, DEAL_ENTRY);
   long   magic      = (long)HistoryDealGetInteger(deal_id, DEAL_MAGIC);
   string symbol     = HistoryDealGetString(deal_id, DEAL_SYMBOL);
   ENUM_DEAL_TYPE dt = (ENUM_DEAL_TYPE)HistoryDealGetInteger(deal_id, DEAL_TYPE);
   double price      = HistoryDealGetDouble(deal_id, DEAL_PRICE);
   double volume     = HistoryDealGetDouble(deal_id, DEAL_VOLUME);
   double profit     = HistoryDealGetDouble(deal_id, DEAL_PROFIT);
   double commission = HistoryDealGetDouble(deal_id, DEAL_COMMISSION);
   double swap       = HistoryDealGetDouble(deal_id, DEAL_SWAP);
   long   deal_time  = (long)HistoryDealGetInteger(deal_id, DEAL_TIME);

   string entry_str = "inout";
   if (entry == DEAL_ENTRY_IN)  entry_str = "in";
   else if (entry == DEAL_ENTRY_OUT) entry_str = "out";

   // Deal type → position direction (BUY/SELL of the OPENING side).
   // For a close deal, the deal type is OPPOSITE of the underlying position
   // direction (BUY-close on a SELL position). We pass the deal's own
   // side; the backend uses mt5_ticket + entry_in to determine the
   // position direction when needed.
   string action = (dt == DEAL_TYPE_BUY) ? "BUY" : "SELL";

   // v1.45 — the broker's REMAINING position volume after this deal.
   // 0.0 = position fully closed; >0 = partial close. The server uses this
   // as the AUTHORITY for partial-vs-full discrimination.
   double pos_vol = 0.0;
   if (PositionSelectByTicket((ulong)position_id))
      pos_vol = PositionGetDouble(POSITION_VOLUME);

   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"mt5_ticket\":%I64d,\"deal_id\":%I64u,"
      "\"deal_entry\":\"%s\",\"symbol\":\"%s\",\"action\":\"%s\","
      "\"lots\":%.2f,\"price\":%.5f,\"profit\":%.2f,"
      "\"commission\":%.2f,\"swap\":%.2f,"
      "\"deal_time\":%I64d,\"magic\":%I64d,\"position_volume\":%.2f}",
      EffectiveToken, position_id, deal_id,
      entry_str, symbol, action,
      volume, price, profit, commission, swap, deal_time, magic, pos_vol);

   HttpPost(g_server_url + "/api/bridge/external-deal", body);
}

//+------------------------------------------------------------------+
//| SweepDealHistory — autonomous catch-up scan.                     |
//|                                                                  |
//| Walks every deal in [lastReportedDealTime .. now] and pushes it  |
//| to /api/bridge/external-deal. The server is idempotent on        |
//| deal_id (unique index on broker_deals), so duplicate pushes are  |
//| harmless no-ops. This is what makes STOIC fully autopilot — even |
//| when OnTradeTransaction misses an event (closed on a different   |
//| terminal, EA reloaded mid-event, network glitch), the sweep      |
//| catches it on the next tick and pushes it through.               |
//+------------------------------------------------------------------+
void SweepDealHistory() {
   datetime from_ts = lastReportedDealTime;
   if (from_ts <= 0) from_ts = TimeCurrent() - HistoryLookbackSeconds;
   datetime to_ts   = TimeCurrent() + 60;   // slight forward fudge in case of clock skew

   if (!HistorySelect(from_ts, to_ts)) return;
   int total = HistoryDealsTotal();
   if (total <= 0) return;

   datetime new_watermark = lastReportedDealTime;
   int pushed = 0;

   for (int i = 0; i < total; i++) {
      if (pushed >= HistorySweepMaxDeals) break;
      ulong deal_id = HistoryDealGetTicket(i);
      if (deal_id == 0) continue;

      long position_id = (long)HistoryDealGetInteger(deal_id, DEAL_POSITION_ID);
      if (position_id == 0) continue;  // skip balance ops, deposits etc.
      long deal_time = (long)HistoryDealGetInteger(deal_id, DEAL_TIME);
      if ((datetime)deal_time <= lastReportedDealTime) continue;

      ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal_id, DEAL_ENTRY);
      long   magic      = (long)HistoryDealGetInteger(deal_id, DEAL_MAGIC);
      string symbol     = HistoryDealGetString(deal_id, DEAL_SYMBOL);
      ENUM_DEAL_TYPE dt = (ENUM_DEAL_TYPE)HistoryDealGetInteger(deal_id, DEAL_TYPE);
      double price      = HistoryDealGetDouble(deal_id, DEAL_PRICE);
      double volume     = HistoryDealGetDouble(deal_id, DEAL_VOLUME);
      double profit     = HistoryDealGetDouble(deal_id, DEAL_PROFIT);
      double commission = HistoryDealGetDouble(deal_id, DEAL_COMMISSION);
      double swap       = HistoryDealGetDouble(deal_id, DEAL_SWAP);

      string entry_str = "inout";
      if (entry == DEAL_ENTRY_IN)       entry_str = "in";
      else if (entry == DEAL_ENTRY_OUT) entry_str = "out";

      string action = (dt == DEAL_TYPE_BUY) ? "BUY" : "SELL";

      string body = StringFormat(
         "{\"bridge_token\":\"%s\",\"mt5_ticket\":%I64d,\"deal_id\":%I64u,"
         "\"deal_entry\":\"%s\",\"symbol\":\"%s\",\"action\":\"%s\","
         "\"lots\":%.2f,\"price\":%.5f,\"profit\":%.2f,"
         "\"commission\":%.2f,\"swap\":%.2f,"
         "\"deal_time\":%I64d,\"magic\":%I64d,\"backfill\":true}",
         EffectiveToken, position_id, deal_id,
         entry_str, symbol, action,
         volume, price, profit, commission, swap, deal_time, magic);

      HttpPost(g_server_url + "/api/bridge/external-deal", body);
      pushed++;
      if ((datetime)deal_time > new_watermark) new_watermark = (datetime)deal_time;
   }

   // Advance the watermark only on successful processing — server idempotency
   // covers duplicates if the EA restarts mid-sweep.
   if (new_watermark > lastReportedDealTime) {
      lastReportedDealTime = new_watermark;
   }
   if (pushed > 0) {
      Print("STOIC history sweep pushed ", pushed, " deals (window ",
            TimeToString(from_ts), " → ", TimeToString(to_ts), ")");
   }
}

//+------------------------------------------------------------------+
//| EA v1.44 — scalp tick stream. Batches every bid/ask tick of       |
//| TickStreamSymbol since the last post to /api/bridge/ticks.        |
//+------------------------------------------------------------------+
void SendTicks() {
   if (StringLen(_tick_symbol) == 0) _tick_symbol = ResolveTickSymbol(TickStreamSymbol);
   if (StringLen(_tick_symbol) == 0 || StringLen(EffectiveToken) == 0) {
      TickStreamWarn("no symbol or bridge token — stream idle");
      return;
   }
   MqlTick last_tick;
   if (!SymbolInfoTick(_tick_symbol, last_tick)) {
      TickStreamWarn("SymbolInfoTick failed for '" + _tick_symbol +
                     "' — check the exact Market Watch name");
      return;
   }
   ulong now_msc = (ulong)last_tick.time_msc;
   if (now_msc == 0) {
      TickStreamWarn("no quote yet for " + _tick_symbol + " (market closed?)");
      return;
   }
   ulong from = (_last_tick_msc == 0) ? now_msc - 2000 : _last_tick_msc + 1;
   if (from > now_msc) return;
   MqlTick ticks[];
   int n = CopyTicksRange(_tick_symbol, ticks, COPY_TICKS_INFO, from, now_msc);
   if (n <= 0) {
      if (n < 0)
         TickStreamWarn("CopyTicksRange error " + (string)GetLastError() +
                        " for " + _tick_symbol);
      return;
   }
   int start = MathMax(0, n - 120);   // cap batch size
   string body = "{\"bridge_token\":\"" + EffectiveToken +
                 "\",\"symbol\":\"" + _tick_symbol +
                 "\",\"sent_at_ms\":" + (string)now_msc + ",\"ticks\":[";
   for (int i = start; i < n; i++) {
      if (i > start) body += ",";
      body += "{\"tm\":" + (string)ticks[i].time_msc +
              ",\"b\":" + DoubleToString(ticks[i].bid, 5) +
              ",\"a\":" + DoubleToString(ticks[i].ask, 5) + "}";
   }
   body += "]}";
   _last_tick_msc = (ulong)ticks[n - 1].time_msc;
   HttpPost(g_server_url + "/api/bridge/ticks", body);
}

//+------------------------------------------------------------------+
string HttpPost(string url, string body) {
   char post[]; char result[]; string headers;
   StringToCharArray(body, post, 0, StringLen(body), CP_UTF8);
   string req_headers = "Content-Type: application/json\r\n";
   ResetLastError();
   int res = WebRequest("POST", url, req_headers, 10000, post, result, headers);
   if (res == -1) {
      int err = GetLastError();
      if (err == 4014) {
         g_webrequest_ok = false;
         Print("WebRequest error 4014 — FIX: Tools → Options → Expert Advisors → tick 'Allow WebRequest for listed URL' and add ", g_server_url);
      } else {
         Print("WebRequest error: ", err, " calling ", url, " (network/DNS? test the URL in a browser on this VPS)");
      }
      return "";
   }
   return CharArrayToString(result, 0, ArraySize(result), CP_UTF8);
}

// Pip size lookup (must mirror backend pip_utils.PIP_SIZE for XAU/BTC/JPY)
double SymbolPipSize(string sym) {
   if (sym == "XAUUSD") return 0.10;
   if (sym == "XAGUSD") return 0.01;
   if (sym == "BTCUSD") return 1.00;
   if (sym == "ETHUSD") return 0.10;
   if (StringFind(sym, "JPY") >= 0) return 0.01;
   return 0.0001;
}

// v1.28 — Broker-supported filling mode picker.
// Different brokers support different filling modes for the same symbol.
// VT Markets, IC Markets and many ECN brokers only support FOK on certain
// symbols, which made the previous hardcoded IOC return retcode 10013
// (INVALID_REQUEST). This queries SYMBOL_FILLING_MODE and picks one the
// broker actually accepts: FOK first (most strict), then IOC, then RETURN.
ENUM_ORDER_TYPE_FILLING PickFillingMode(string sym) {
   long modes = SymbolInfoInteger(sym, SYMBOL_FILLING_MODE);
   if ((modes & SYMBOL_FILLING_FOK) != 0) return ORDER_FILLING_FOK;
   if ((modes & SYMBOL_FILLING_IOC) != 0) return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
}

// v1.29 — Resolve the broker's actual symbol name for a base symbol.
// Brokers rename instruments with suffixes (XAUUSD.x, XAUUSD.raw, XAUUSDpro,
// XAUUSD.ecn, XAUUSD+, XAUUSDm, XAUUSD#, XAUUSD-PRO, etc.). When STOIC sends
// "XAUUSD" but the terminal only knows "XAUUSD.x", SymbolInfoDouble returns 0
// and OrderSend returns retcode 10013 (INVALID_REQUEST). This helper tries
// the bare symbol then every common suffix, returning the first one that
// has a valid live tick AND can be selected into MarketWatch. If nothing
// resolves, returns "" so the caller can abort with a clear error.
string ResolveBrokerSymbol(string base_symbol) {
   // Try bare symbol first — most accounts will hit this path.
   if (SymbolSelect(base_symbol, true)) {
      MqlTick tick;
      if (SymbolInfoTick(base_symbol, tick) && tick.bid > 0 && tick.ask > 0) {
         return base_symbol;
      }
   }
   // Common broker suffix variants observed in the wild.
   // v1.31 — added .c/.cent (Tauro/JMFinancial cent accounts), .s/.std
   //         (FBS/Roboforex standard), .i (IC Markets institutional), ~ (some ECN),
   //         ".spot" (Vantage), "_x"/"_raw"/"_ecn" (underscore-style brokers).
   // v1.32 — added .fx/.FX (Tauro Markets / JMFinancial-Server demo accounts).
   // v1.33 — added .e/.E (OnEquity ECN accounts).
   string suffixes[] = {
      ".x", ".X", ".raw", ".RAW", ".r", ".m", ".ecn", ".ECN",
      "pro", "Pro", "PRO", "+", "#", "m", "_pro", "-ECN", ".pro",
      ".c", ".C", ".cent", "cent", ".s", ".S", ".std", ".STD",
      ".i", ".I", "~", ".spot", "_x", "_raw", "_ecn",
      ".fx", ".FX", ".Fx", ".e", ".E"
   };
   for (int i = 0; i < ArraySize(suffixes); i++) {
      string candidate = base_symbol + suffixes[i];
      if (SymbolSelect(candidate, true)) {
         MqlTick tick;
         if (SymbolInfoTick(candidate, tick) && tick.bid > 0 && tick.ask > 0) {
            Print("[v1.31] ResolveBrokerSymbol: ", base_symbol, " -> ", candidate);
            return candidate;
         }
      }
   }
   return "";  // signal "not found"
}

// Build {"XAUUSD":3.2,"BTCUSD":85.0} from the comma list, using current symbol spread
string BuildSpreadsJson() {
   string out = "{";
   string list = TrackedSymbols;
   bool first = true;
   int start = 0;
   for (int i = 0; i <= StringLen(list); i++) {
      if (i == StringLen(list) || StringGetCharacter(list, i) == ',') {
         string sym = StringSubstr(list, start, i - start);
         StringTrimLeft(sym); StringTrimRight(sym);
         if (StringLen(sym) > 0) {
            MqlTick tick;
            if (SymbolInfoTick(sym, tick) && tick.ask > 0 && tick.bid > 0) {
               double pip = SymbolPipSize(sym);
               double spread_pips = (pip > 0) ? (tick.ask - tick.bid) / pip : 0.0;
               if (!first) out += ",";
               out += StringFormat("\"%s\":%.2f", sym, spread_pips);
               first = false;
            }
         }
         start = i + 1;
      }
   }
   out += "}";
   return out;
}

// Build [{"ticket":...,"symbol":"...","type":"BUY","volume":1.0,
//          "price_open":...,"sl":...,"tp":...,"time_open":...,
//          "magic":...,"profit":...}, ...] for every currently-open position.
// Backend ingests this snapshot to auto-create STOIC trade records for
// positions opened BEFORE the EA was attached or opened manually on MT5.
string BuildPositionsJson() {
   int total = PositionsTotal();
   string out = "[";
   bool first = true;
   for (int i = 0; i < total; i++) {
      ulong ticket = PositionGetTicket(i);
      if (ticket == 0) continue;
      if (!PositionSelectByTicket(ticket)) continue;
      string sym       = PositionGetString(POSITION_SYMBOL);
      long   ptype     = PositionGetInteger(POSITION_TYPE);   // 0=BUY,1=SELL
      double volume    = PositionGetDouble(POSITION_VOLUME);
      double priceOpen = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl        = PositionGetDouble(POSITION_SL);
      double tp        = PositionGetDouble(POSITION_TP);
      long   timeOpen  = (long)PositionGetInteger(POSITION_TIME);
      long   magic     = (long)PositionGetInteger(POSITION_MAGIC);
      double profit    = PositionGetDouble(POSITION_PROFIT);
      double priceCur  = PositionGetDouble(POSITION_PRICE_CURRENT);  // v1.27 — broker-live tick
      string typeStr   = (ptype == POSITION_TYPE_BUY) ? "BUY" : "SELL";
      if (!first) out += ",";
      out += StringFormat(
         "{\"ticket\":%I64u,\"symbol\":\"%s\",\"type\":\"%s\","
         "\"volume\":%.2f,\"price_open\":%.5f,\"sl\":%.5f,\"tp\":%.5f,"
         "\"time_open\":%I64d,\"magic\":%I64d,\"profit\":%.2f,"
         "\"current_price\":%.5f}",
         ticket, sym, typeStr, volume, priceOpen, sl, tp, timeOpen, magic, profit,
         priceCur);
      first = false;
   }
   out += "]";
   return out;
}

//+------------------------------------------------------------------+
//| v1.34 · BuildAvailableSymbolsJson                                |
//|                                                                  |
//| Enumerate ALL symbols in MarketWatch (selected=true) and emit a  |
//| JSON array of names whose core matches one of the bases STOIC    |
//| cares about (XAU/BTC/forex majors). The backend's                |
//| broker_symbol_detector turns this into an auto-detected suffix   |
//| so the user never has to manually set symbol_suffix per broker.  |
//|                                                                  |
//| Throttled: only emitted on the FIRST heartbeat after EA start    |
//| AND once per hour after that — symbol lists almost never change. |
//+------------------------------------------------------------------+
datetime _last_symbols_emit = 0;
// EA v1.35: track boot time so the symbols-emit throttle can be relaxed
// during the first 10 minutes after EA attach (rapid discovery), then
// settle into the once/hour baseline. Initialised in OnInit().
datetime _ea_boot_time = 0;
string CACHED_AVAILABLE_SYMBOLS = "";

string BuildAvailableSymbolsJson() {
   // iter-92 · Added broker-alias bases (GOLD, SILVER, US30, DAX40, NAS,
   // DOW, DAX) so brokers like OnEquity/ICMR/PepperstoneRazor that publish
   // gold as `GOLD#`/`SILVER#` instead of `XAUUSD#` are captured in the
   // MarketWatch scan. Backend's broker_symbol_detector.BASE_ALIASES maps
   // GOLD → XAUUSD, SILVER → XAGUSD, US30 → NAS100 (same alias family).
   string bases[] = {
      "XAUUSD","XAGUSD","GOLD","SILVER","BTCUSD","ETHUSD",
      "EURUSD","GBPUSD","USDJPY","USDCHF","USDCAD","AUDUSD","NZDUSD",
      "EURGBP","EURJPY","GBPJPY",
      "USOIL","UKOIL","WTI","BRENT",
      "NAS100","SPX500","GER40","UK100","JPN225","US30","DAX","DAX40","NAS","DOW"
   };
   int total = SymbolsTotal(true);   // true = MarketWatch only
   string out = "[";
   bool first = true;
   for (int i = 0; i < total; i++) {
      string name = SymbolName(i, true);
      if (name == "") continue;
      // Cheap prefix match: does this symbol start with any known base?
      string upper = name;
      StringToUpper(upper);
      bool matched = false;
      for (int b = 0; b < ArraySize(bases); b++) {
         if (StringFind(upper, bases[b]) == 0) { matched = true; break; }
      }
      if (!matched) continue;
      if (!first) out += ",";
      // JSON-safe: replace " with nothing (no broker uses quotes in symbol names)
      StringReplace(name, "\"", "");
      out += "\"" + name + "\"";
      first = false;
   }
   out += "]";
   return out;
}

// v1.48 — {"EURUSD":{"point":0.00001,"digits":5,"stops_level_points":10,
//          "freeze_level_points":0}, ...} for the chart symbol, every
// TrackedSymbols entry and every open-position symbol. Backend's
// protection_guard uses these to respect precise broker stop constraints.
// v1.60 / N98-6 — ACCOUNT_TRADE_MODE → "demo" | "real" | "contest"
string TradeModeString() {
   ENUM_ACCOUNT_TRADE_MODE m = (ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if (m == ACCOUNT_TRADE_MODE_DEMO)    return "demo";
   if (m == ACCOUNT_TRADE_MODE_CONTEST) return "contest";
   return "real";
}

void AppendSymbolSpec(string &json, string sym, bool &first) {
   if (sym == "" || StringFind(json, "\"" + sym + "\":") >= 0) return;
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);
   if (point <= 0) return;
   long digits  = SymbolInfoInteger(sym, SYMBOL_DIGITS);
   long stops   = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   long freeze  = SymbolInfoInteger(sym, SYMBOL_TRADE_FREEZE_LEVEL);
   long tmode   = SymbolInfoInteger(sym, SYMBOL_TRADE_MODE);
   // v1.54 — full contract specs for broker certification.
   double tick_size  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   double tick_value = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double contract   = SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE);
   double vol_min    = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double vol_max    = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double vol_step   = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   if (!first) json += ",";
   json += StringFormat(
      "\"%s\":{\"point\":%.8f,\"digits\":%I64d,"
      "\"stops_level_points\":%I64d,\"freeze_level_points\":%I64d,"
      "\"trade_mode\":%I64d,"
      "\"tick_size\":%.8f,\"tick_value\":%.5f,\"contract_size\":%.2f,"
      "\"volume_min\":%.4f,\"volume_max\":%.2f,\"volume_step\":%.4f}",
      sym, point, digits, stops, freeze, tmode,
      tick_size, tick_value, contract, vol_min, vol_max, vol_step);
   first = false;
}

string BuildSymbolSpecsJson() {
   string out = "{";
   bool first = true;
   AppendSymbolSpec(out, _Symbol, first);
   // TrackedSymbols comma list
   string list = TrackedSymbols;
   int start = 0;
   for (int i = 0; i <= StringLen(list); i++) {
      if (i == StringLen(list) || StringGetCharacter(list, i) == ',') {
         string sym = StringSubstr(list, start, i - start);
         StringTrimLeft(sym); StringTrimRight(sym);
         if (StringLen(sym) > 0) AppendSymbolSpec(out, sym, first);
         start = i + 1;
      }
   }
   // every open-position symbol (these are where stops actually get placed)
   int totalPos = PositionsTotal();
   for (int p = 0; p < totalPos; p++) {
      ulong specTicket = PositionGetTicket(p);
      if (specTicket == 0 || !PositionSelectByTicket(specTicket)) continue;
      AppendSymbolSpec(out, PositionGetString(POSITION_SYMBOL), first);
   }
   out += "}";
   return out;
}


// v1.54 — broker server time / DST / trading-session facts so the backend
//          can certify DST handling from observed evidence (correction #5).
string BuildBrokerTimeJson() {
   long srv_offset = (long)(TimeTradeServer() - TimeGMT());
   datetime from_t, to_t;
   string sessions = "[";
   bool first = true;
   MqlDateTime st;
   TimeToStruct(TimeTradeServer(), st);
   ENUM_DAY_OF_WEEK dow = (ENUM_DAY_OF_WEEK)st.day_of_week;
   for (uint sess = 0; sess < 8; sess++) {
      if (!SymbolInfoSessionTrade(_Symbol, dow, sess, from_t, to_t)) break;
      if (!first) sessions += ",";
      sessions += StringFormat("[%I64d,%I64d]", (long)from_t, (long)to_t);
      first = false;
   }
   sessions += "]";
   return StringFormat(
      "{\"server_gmt_offset_sec\":%I64d,\"server_time\":%I64d,"
      "\"symbol\":\"%s\",\"trade_sessions_today\":%s}",
      srv_offset, (long)TimeTradeServer(), _Symbol, sessions);
}

void SendHeartbeat() {
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
   int    openPos = PositionsTotal();
   string spreads = BuildSpreadsJson();
   // EA v1.24: include broker-side account login + currency so STOIC can
   // detect "wrong MT5 terminal" misconfigurations.
   long   login  = (long)AccountInfoInteger(ACCOUNT_LOGIN);
   string ccy    = AccountInfoString(ACCOUNT_CURRENCY);
   // EA v1.25: include the full live positions snapshot so STOIC can
   // backfill pre-existing trades it never saw via OnTradeTransaction.
   string positions = BuildPositionsJson();
   // EA v1.26: report our own semantic version so the Dashboard can flag
   // stale terminals (no manual MT5 inspection required).
   // EA v1.29: route the version through %s so the literal can never drift
   // from EA_CLIENT_VERSION (previous hardcoded "1.28" caused stale dashboards).
   // EA v1.34: include MarketWatch symbol inventory.
   // EA v1.35: faster suffix-discovery — emit symbols every PollSeconds for
   //   the first 600s after EA boot (so the backend knows the broker's
   //   symbol layout within a minute of attaching the EA to a new account),
   //   then throttle to once per hour to keep heartbeat payloads small.
   datetime now_t = TimeCurrent();
   long age = (long)(now_t - _ea_boot_time);
   long throttle = (age < 600) ? 0 : 3600;  // 0 = every heartbeat for first 10 min
   if (now_t - _last_symbols_emit >= throttle || _last_symbols_emit == 0) {
      CACHED_AVAILABLE_SYMBOLS = BuildAvailableSymbolsJson();
      _last_symbols_emit = now_t;
   }
   // EA v1.55: mandatory identity block — broker server, installation id,
   // terminal build and ea_version ride on EVERY heartbeat so the server
   // can verify the full identity chain (unverified = telemetry only).
   string broker_srv = AccountInfoString(ACCOUNT_SERVER);
   int term_build = (int)TerminalInfoInteger(TERMINAL_BUILD);
   // H1 — the server must know whether this account NETS positions (one
   // broker position per symbol) or hedges; report ACCOUNT_MARGIN_MODE.
   string margin_mode = ((ENUM_ACCOUNT_MARGIN_MODE)AccountInfoInteger(ACCOUNT_MARGIN_MODE)
                         == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING) ? "hedging" : "netting";
   // v1.60 / N98-6 — the broker's own word on practice vs real money. The
   // server trusts a DEMO classification only when this says "demo".
   string trade_mode = TradeModeString();
   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"balance\":%.2f,\"equity\":%.2f,"
      "\"open_positions\":%d,\"spreads\":%s,"
      "\"account_login\":%I64d,\"base_currency\":\"%s\","
      "\"margin_mode\":\"%s\",\"trade_mode\":\"%s\","
      "\"broker_server\":\"%s\",\"installation_id\":\"%s\","
      "\"terminal_build\":%d,\"ea_version\":\"%s\","
      "\"ea_binary_sha256\":\"%s\","
      "\"client_time_ms\":%I64d,"
      "\"positions\":%s,\"client_version\":\"%s\","
      "\"symbol_specs\":%s,"
      "\"broker_time\":%s,"
      "\"available_symbols\":%s,"
      "\"ea_self_check\":{\"autotrading\":%s,\"ea_trade_allowed\":%s,\"webrequest_ok\":%s}}",
      EffectiveToken, balance, equity, openPos, spreads, login, ccy,
      margin_mode, trade_mode,
      broker_srv, EffectiveInstallation, term_build, EA_CLIENT_VERSION,
      EffectiveProofHash,
      (long)TimeGMT() * 1000,
      positions, EA_CLIENT_VERSION, BuildSymbolSpecsJson(),
      BuildBrokerTimeJson(), CACHED_AVAILABLE_SYMBOLS,
      (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ? "true" : "false"),
      (MQLInfoInteger(MQL_TRADE_ALLOWED) ? "true" : "false"),
      (g_webrequest_ok ? "true" : "false"));
   string hb = HttpPost(g_server_url + "/api/bridge/heartbeat", body);
   if (StringLen(hb) > 0) g_webrequest_ok = true;
}

void PollPendingTrades() {
   // v1.41 — skip the poll entirely during the EOD quiet window so no
   // open/modify/close is even fetched; the server re-dispatches after.
   if (IsEodQuietWindow()) return;
   string body = StringFormat("{\"bridge_token\":\"%s\"}", EffectiveToken);
   string resp = HttpPost(g_server_url + "/api/bridge/poll-trades", body);
   if (StringLen(resp) == 0) return;

   // -------- 1. Process pending NEW trades from "trades":[...] block --------
   ParseTradesBlock(resp);

   // -------- 2. Process pending modifications from "modifications":[...] block --------
   ParseModificationsBlock(resp);

   // -------- 3. v1.39: server-requested deep broker-history sync --------
   ParseSyncRequest(resp);
}

// ----- v1.39: DEEP BROKER SYNC -----
// The server sets "sync_request":{"lookback_seconds":N} on the poll-trades
// response when the user clicks SYNC on the dashboard (or auto-heal detects
// trades with estimated/missing P&L). We re-scan the FULL deal history for
// the window — ignoring the incremental sweep watermark — and push every
// deal. Server-side idempotency (unique deal_id index) makes duplicate
// pushes harmless, while inexact trade records get repaired with the exact
// broker figures. Completion is confirmed via /api/bridge/sync-complete.
datetime _last_deep_sync = 0;

void ParseSyncRequest(string resp) {
   int s = StringFind(resp, "\"sync_request\":{");
   if (s < 0) return;
   // Local guard: never run two deep syncs within 60s even if the server
   // re-dispatches (its own re-dispatch window is 180s).
   if (_last_deep_sync > 0 && TimeCurrent() - _last_deep_sync < 60) return;
   long lookback = (long)ExtractDouble(resp, "\"lookback_seconds\":", s);
   if (lookback <= 0) lookback = 604800;   // default 7 days
   _last_deep_sync = TimeCurrent();
   DeepSyncHistory((int)lookback);
}

// Push a single (already history-selected) deal to /external-deal.
// Returns true if the deal belonged to a position and got posted.
bool PushDealById(ulong deal_id) {
   long position_id = (long)HistoryDealGetInteger(deal_id, DEAL_POSITION_ID);
   if (position_id == 0) return false;   // balance ops, deposits etc.
   ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal_id, DEAL_ENTRY);
   long   magic      = (long)HistoryDealGetInteger(deal_id, DEAL_MAGIC);
   string symbol     = HistoryDealGetString(deal_id, DEAL_SYMBOL);
   ENUM_DEAL_TYPE dt = (ENUM_DEAL_TYPE)HistoryDealGetInteger(deal_id, DEAL_TYPE);
   double price      = HistoryDealGetDouble(deal_id, DEAL_PRICE);
   double volume     = HistoryDealGetDouble(deal_id, DEAL_VOLUME);
   double profit     = HistoryDealGetDouble(deal_id, DEAL_PROFIT);
   double commission = HistoryDealGetDouble(deal_id, DEAL_COMMISSION);
   double swap       = HistoryDealGetDouble(deal_id, DEAL_SWAP);
   long   deal_time  = (long)HistoryDealGetInteger(deal_id, DEAL_TIME);

   string entry_str = "inout";
   if (entry == DEAL_ENTRY_IN)       entry_str = "in";
   else if (entry == DEAL_ENTRY_OUT) entry_str = "out";
   string action = (dt == DEAL_TYPE_BUY) ? "BUY" : "SELL";

   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"mt5_ticket\":%I64d,\"deal_id\":%I64u,"
      "\"deal_entry\":\"%s\",\"symbol\":\"%s\",\"action\":\"%s\","
      "\"lots\":%.2f,\"price\":%.5f,\"profit\":%.2f,"
      "\"commission\":%.2f,\"swap\":%.2f,"
      "\"deal_time\":%I64d,\"magic\":%I64d,\"backfill\":true}",
      EffectiveToken, position_id, deal_id,
      entry_str, symbol, action,
      volume, price, profit, commission, swap, deal_time, magic);
   HttpPost(g_server_url + "/api/bridge/external-deal", body);
   return true;
}

void DeepSyncHistory(int lookback_seconds) {
   datetime from_ts = TimeCurrent() - lookback_seconds;
   datetime to_ts   = TimeCurrent() + 60;
   int pushed = 0;
   if (HistorySelect(from_ts, to_ts)) {
      int total = HistoryDealsTotal();
      Print("STOIC deep-sync: re-scanning ", total, " deals over last ",
            lookback_seconds / 86400, " day(s)");
      for (int i = 0; i < total; i++) {
         if (pushed >= 400) break;   // hard cap per sync pass
         ulong deal_id = HistoryDealGetTicket(i);
         if (deal_id == 0) continue;
         if (PushDealById(deal_id)) pushed++;
      }
   } else {
      Print("STOIC deep-sync: HistorySelect failed (", GetLastError(), ")");
   }
   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"deals_pushed\":%d,\"lookback_seconds\":%d}",
      EffectiveToken, pushed, lookback_seconds);
   HttpPost(g_server_url + "/api/bridge/sync-complete", body);
   Print("STOIC deep-sync complete: pushed ", pushed, " deals.");
}

// ----- TRADES BLOCK -----
void ParseTradesBlock(string resp) {
   int trades_section = StringFind(resp, "\"trades\":[");
   if (trades_section < 0) return;
   int section_end = StringFind(resp, "]", trades_section);
   if (section_end < 0) return;
   string section = StringSubstr(resp, trades_section, section_end - trades_section);

   int idx = 0;
   while (true) {
      int t_start = StringFind(section, "\"trade_id\":\"", idx);
      if (t_start < 0) break;
      t_start += 12;
      int t_end = StringFind(section, "\"", t_start);
      string trade_id = StringSubstr(section, t_start, t_end - t_start);

      string symbol = ExtractString(section, "\"symbol\":\"", t_end);
      string action = ExtractString(section, "\"action\":\"", t_end);
      double lot    = ExtractDouble(section, "\"lot_size\":", t_end);
      double sl     = ExtractDouble(section, "\"stop_loss\":", t_end);
      double tp     = ExtractDouble(section, "\"take_profit\":", t_end);
      int close_pos = StringFind(section, "\"close_requested\":true", t_end);
      int brace_pos = StringFind(section, "}", t_end);
      bool   close_req = (close_pos > 0 && close_pos < brace_pos);
      long ticket   = (long)ExtractDouble(section, "\"mt5_ticket\":", t_end);
      // v1.57 (audit r17 P0-01) — NL close commands carry a fenced idempotency
      // key: the terminal DURABLY dedupes the exact key and rejects a lower fence
      // for the same trade BEFORE OrderSend.
      string nl_key   = ExtractString(section, "\"close_idem_key\":\"", t_end);
      int    key_pos  = StringFind(section, "\"close_idem_key\":\"", t_end);
      if (key_pos < 0 || key_pos > brace_pos) nl_key = "";
      // r18 P0-01: close_seq is the backend's DURABLE per-trade command sequence
      // (monotonic across proposals, PANIC and recovery). The proposal-local
      // fence is NOT used for ordering here — a newer proposal always wins.
      long   nl_seq  = (long)ExtractDouble(section, "\"close_seq\":", t_end);
      int    seq_pos = StringFind(section, "\"close_seq\":", t_end);
      if (seq_pos < 0 || seq_pos > brace_pos) nl_seq = 0;

      if (close_req && ticket > 0) {
         if (!NlCloseAdmitted(trade_id, nl_key, nl_seq)) {
            idx = brace_pos + 1;
            if (idx <= 0) break;
            continue;
         }
         ClosePosition(trade_id, ticket);
         if (StringLen(nl_key) > 0 && IntentDone("close-" + trade_id))
            GlobalVariableSet(JKey("I", "nlkey-" + nl_key), 1);
      } else if (ticket == 0) {
         ExecuteTrade(trade_id, symbol, action, lot, sl, tp);
      }

      idx = brace_pos + 1;
      if (idx <= 0) break;
   }
}

// v1.57/r18 — destination-side NL close admission (audit r17 P0-01, r18 P0-01).
// Persisted in the terminal's Global Variables (survive restarts): the highest
// close_seq seen per trade and every idempotency key already executed.
// close_seq is a durable cross-proposal sequence owned by the backend, so a
// genuinely newer command can never be rejected because an OLDER proposal had
// more worker recovery attempts.
bool NlCloseAdmitted(string trade_id, string nl_key, long nl_seq) {
   if (StringLen(nl_key) > 0 && IntentDone("nlkey-" + nl_key)) {
      Print("STOIC v1.57: duplicate NL close key ignored trade=", trade_id);
      return false;
   }
   if (nl_seq > 0) {
      string sk = JKey("S", trade_id);
      long seen = GlobalVariableCheck(sk) ? (long)GlobalVariableGet(sk) : 0;
      if (nl_seq < seen) {
         Print("STOIC v1.57: STALE NL close_seq rejected trade=", trade_id,
               " seq=", nl_seq, " seen=", seen);
         return false;
      }
      if (nl_seq > seen) GlobalVariableSet(sk, (double)nl_seq);
   }
   return true;
}

// ----- MODIFICATIONS BLOCK -----
void ParseModificationsBlock(string resp) {
   int section_start = StringFind(resp, "\"modifications\":[");
   if (section_start < 0) return;
   int section_end = StringFind(resp, "]", section_start);
   if (section_end < 0) return;
   string section = StringSubstr(resp, section_start, section_end - section_start);

   int idx = 0;
   while (true) {
      int t_start = StringFind(section, "\"trade_id\":\"", idx);
      if (t_start < 0) break;
      t_start += 12;
      int t_end = StringFind(section, "\"", t_start);
      string trade_id = StringSubstr(section, t_start, t_end - t_start);

      int brace_pos = StringFind(section, "}", t_end);
      // v1.50 — extract keys ONLY inside this object so a null intent_id
      // here can never pick up the next object's quoted value.
      int obj_len = (brace_pos > t_end) ? brace_pos - t_end
                                        : StringLen(section) - t_end;
      string obj = StringSubstr(section, t_end, obj_len);

      string mod_type = ExtractString(obj, "\"type\":\"", 0);
      long   ticket   = (long)ExtractDouble(obj, "\"mt5_ticket\":", 0);
      double new_sl   = ExtractDouble(obj, "\"new_sl\":", 0);
      double new_vol  = ExtractDouble(obj, "\"new_volume\":", 0);
      string intent   = ExtractString(obj, "\"intent_id\":\"", 0);
      long   seq      = (long)ExtractDouble(obj, "\"seq\":", 0);

      if (StringLen(intent) > 0 && IntentDone(intent)) {
         // v1.50 fence — already executed: replay the JOURNALED outcome
         // (never a generic success) plus live position facts. NEVER
         // execute twice.
         double outc = GlobalVariableGet(JKey("I", intent));
         bool osucc = (outc != 2);
         string extra = "";
         if (ticket > 0 && PositionSelectByTicket(ticket)) {
            if (mod_type == "MODIFY_SL")
               extra = StringFormat("\"confirmed_position_sl\":%.5f,",
                                    PositionGetDouble(POSITION_SL));
            else if (mod_type == "PARTIAL_CLOSE")
               extra = StringFormat("\"remaining_volume\":%.2f,",
                                    PositionGetDouble(POSITION_VOLUME));
         }
         string rebody = StringFormat(
            "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"%s\",\"success\":%s,%s%s\"replay\":true,\"intent_id\":\"%s\",\"error\":\"intent_already_executed\"}",
            EffectiveToken, trade_id, mod_type,
            (osucc ? "true" : "false"),
            (osucc ? "" : "\"terminal\":true,"), extra, intent);
         HttpPost(g_server_url + "/api/bridge/modification-ack", rebody);
      } else if (seq > 0 && seq <= (long)JGet("S", trade_id)) {
         // v1.50 fence — superseded by a newer executed command: send a
         // structured TERMINAL ack so the server clears the queue entry
         // without ever treating it as executed.
         string sbody = StringFormat(
            "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"%s\",\"success\":false,\"superseded\":true,\"intent_id\":\"%s\",\"received_seq\":%I64d,\"latest_seq\":%I64d,\"error\":\"stale_sequence\"}",
            EffectiveToken, trade_id, mod_type, intent, seq,
            (long)JGet("S", trade_id));
         HttpPost(g_server_url + "/api/bridge/modification-ack", sbody);
         Print("STOIC v1.50: superseded stale command seq=", seq, " trade=",
               trade_id, " newest=", (long)JGet("S", trade_id));
      } else if (mod_type == "MODIFY_SL" && ticket > 0 && new_sl > 0) {
         ApplyModifySL(trade_id, ticket, new_sl, intent, seq);
      } else if (mod_type == "PARTIAL_CLOSE" && ticket > 0 && new_vol > 0) {
         // v1.50 — combo (PC + SL) is no longer chained here: the server
         // enqueues a SEPARATE fenced MODIFY_SL after the PC ack succeeds,
         // so each durable command has its own intent + seq.
         ApplyPartialClose(trade_id, ticket, new_vol, intent, seq);
      } else if (mod_type == "FULL_CLOSE" && ticket > 0) {
         // v1.40 — slippage veto / auto-deleverage / reconciler force-close.
         ApplyFullClose(trade_id, ticket, intent, seq);
      }

      idx = brace_pos + 1;
      if (idx <= 0) break;
   }
}

string ExtractString(string src, string key, int from_pos) {
   int s = StringFind(src, key, from_pos);
   if (s < 0) return "";
   s += StringLen(key);
   int e = StringFind(src, "\"", s);
   if (e < 0) return "";
   return StringSubstr(src, s, e - s);
}

double ExtractDouble(string src, string key, int from_pos) {
   int s = StringFind(src, key, from_pos);
   if (s < 0) return 0;
   s += StringLen(key);
   int e = s;
   while (e < StringLen(src)) {
      ushort c = StringGetCharacter(src, e);
      if (c == ',' || c == '}') break;
      e++;
   }
   string num = StringSubstr(src, s, e - s);
   return StringToDouble(num);
}

//+------------------------------------------------------------------+
//| v1.38 — Clamp SL/TP to the broker's minimum stop distance so      |
//| OrderSend never fails with retcode 10016 (INVALID_STOPS).         |
//| `side`: +1 for BUY, -1 for SELL. `extra_mult` widens the safety   |
//| buffer (the 10016 retry pass uses 2).                             |
//| MT5 rule: for a BUY position both SL and TP are compared against  |
//| Bid; for a SELL position against Ask.                             |
//+------------------------------------------------------------------+
void ClampStops(string sym, int side, double &sl, double &tp, int extra_mult) {
   int    digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point  = SymbolInfoDouble(sym, SYMBOL_POINT);
   long   stops  = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   long   freeze = SymbolInfoInteger(sym, SYMBOL_TRADE_FREEZE_LEVEL);
   long   pts    = (stops > freeze ? stops : freeze) + 2;   // +2pt safety
   double min_dist = pts * point * extra_mult;

   double bid = SymbolInfoDouble(sym, SYMBOL_BID);
   double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
   // Some brokers report stops_level=0 but still enforce a spread-based
   // distance internally — never go tighter than the live spread.
   double spread = ask - bid;
   if (min_dist < spread * extra_mult) min_dist = spread * extra_mult;

   if (side > 0) {                       // BUY — measured against Bid
      if (sl > 0 && sl > bid - min_dist) sl = bid - min_dist;
      if (tp > 0 && tp < bid + min_dist) tp = bid + min_dist;
   } else {                              // SELL — measured against Ask
      if (sl > 0 && sl < ask + min_dist) sl = ask + min_dist;
      if (tp > 0 && tp > ask - min_dist) tp = ask - min_dist;
   }
   sl = (sl > 0) ? NormalizeDouble(sl, digits) : 0;
   tp = (tp > 0) ? NormalizeDouble(tp, digits) : 0;
}

// v1.50 — one JSON emitter for every open-order report (normal + replay).
void SendOpenReport(string trade_id, ulong ticket, string status,
                    double entry, double req_price, string err,
                    double req_sl, double app_sl, double conf_sl,
                    bool replay,
                    ulong order_tk = 0, ulong deal_tk = 0,
                    ulong pos_id = 0, double filled = 0,
                    bool partial = false, double pos_volume = 0) {
   // v1.52 — mt5_ticket carries the POSITION identifier; order_ticket /
   // deal_ticket / position_id are reported separately (order != position,
   // especially on netting accounts).
   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"mt5_ticket\":%I64u,"
      "\"status\":\"%s\",\"entry_price\":%.5f,\"requested_price\":%.5f,"
      "\"requested_sl\":%.5f,\"applied_sl\":%.5f,\"confirmed_position_sl\":%.5f,"
      "\"order_ticket\":%I64u,\"deal_ticket\":%I64u,\"position_id\":%I64u,"
      "\"filled_volume\":%.4f,\"partial_fill\":%s,\"position_volume\":%.4f,"
      "\"replay\":%s,\"error\":\"%s\"}",
      EffectiveToken, trade_id, ticket, status, entry, req_price,
      req_sl, app_sl, conf_sl, order_tk, deal_tk, pos_id, filled,
      (partial ? "true" : "false"), pos_volume,
      (replay ? "true" : "false"), err);
   HttpPost(g_server_url + "/api/bridge/report", body);
}

// v1.50 — re-report a journaled result on redispatch (ack was lost).
void ReportOpenFromJournal(string trade_id) {
   ulong  order_tk = JGetTicket("K", trade_id);
   ulong  deal_tk  = JGetTicket("D", trade_id);
   ulong  pos_id   = JGetTicket("Q", trade_id);
   double price    = JGet("P", trade_id);
   double filled   = JGet("V", trade_id);
   ulong  ticket   = (pos_id > 0 ? pos_id : order_tk);
   double conf_sl = 0, pos_volume = 0;
   if (ticket > 0 && SelectPositionById(ticket, "")) {
      conf_sl = PositionGetDouble(POSITION_SL);
      pos_volume = PositionGetDouble(POSITION_VOLUME);
   }
   SendOpenReport(trade_id, ticket, "open", price, 0, "", 0, 0, conf_sl, true,
                  order_tk, deal_tk, pos_id, filled, false, pos_volume);
   JSet("T", trade_id, JR_ACK_SENT);
   Print("STOIC v1.52: journal replay report trade=", trade_id,
         " ticket=", ticket);
}

// v1.51 — shared broker-native preflight for the close/modify paths: MT5
// itself validates margin, volume limits, stop/freeze levels and fill
// policy BEFORE the request reaches the broker. On failure perr carries a
// structured preflight_failed error for the ack payload.
bool PreflightOk(MqlTradeRequest &req, string &perr) {
   MqlTradeCheckResult chk;
   ZeroMemory(chk);
   if (OrderCheck(req, chk)) return true;
   string c = chk.comment;
   StringReplace(c, "\"", "'");   // keep the ack JSON body valid
   perr = StringFormat("preflight_failed:retcode=%d:%s", chk.retcode, c);
   Print("STOIC v1.51: OrderCheck preflight REJECTED ", req.symbol,
         " action=", (int)req.action, " retcode=", chk.retcode,
         " comment=", chk.comment, " margin=", chk.margin,
         " free=", chk.margin_free);
   return false;
}

void ExecuteTrade(string trade_id, string symbol, string action, double lot, double sl, double tp) {
   if (IsEodQuietWindow()) return;   // v1.41 — deferred, server re-dispatches

   // v1.50 — durable intent journal: a redispatched trade_id returns the
   // PREVIOUS result; a second OrderSend for the same trade_id is impossible.
   double jstate = JGet("T", trade_id);
   // v1.53 — BROKER_ACCEPTED_UNRESOLVED redispatch: re-run position
   // resolution; NEVER resend the order.
   if (jstate == JR_ACCEPTED) {
      string rsym = ResolveBrokerSymbol(symbol);
      ulong pid = PositionIdFromDeal(JGetTicket("D", trade_id));
      if (pid == 0)
         pid = FindPositionForTrade(trade_id, rsym, action, lot);
      if (pid > 0 && SelectPositionById(pid, rsym)) {
         JSetTicket("Q", trade_id, pid);
         JSet("P", trade_id, PositionGetDouble(POSITION_PRICE_OPEN));
         JSet("V", trade_id, OrderFilledVolume(JGetTicket("K", trade_id)));
         JSet("T", trade_id, JR_TICKET);
         ReportOpenFromJournal(trade_id);
         return;
      }
      SendOpenReport(trade_id, 0, "pending", 0, 0, "accepted_unresolved",
                     0, 0, 0, true, JGetTicket("K", trade_id),
                     JGetTicket("D", trade_id), 0, 0, false, 0);
      return;
   }
   if (jstate >= JR_TICKET && jstate != JR_FAILED
       && jstate != JR_ACCEPTED) {
      ReportOpenFromJournal(trade_id);
      return;
   }
   if (jstate == JR_FAILED) {
      SendOpenReport(trade_id, 0, "failed", 0, 0, "journal_failed_replay",
                     0, 0, 0, true);
      return;
   }
   if (jstate == JR_ORDER_SENT) {
      // Crash window: order was sent but the result never journaled.
      // v1.52 — netting-aware recovery: comment scan, then order/deal
      // HISTORY by magic + comment + symbol + side + volume + time window.
      // NEVER resend blindly.
      string rec_symbol = ResolveBrokerSymbol(symbol);
      ulong rec_pos = FindPositionForTrade(trade_id, rec_symbol, action, lot);
      if (rec_pos > 0 && SelectPositionById(rec_pos, rec_symbol)) {
         JSetTicket("Q", trade_id, rec_pos);
         JSet("P", trade_id, PositionGetDouble(POSITION_PRICE_OPEN));
         JSet("V", trade_id, PositionGetDouble(POSITION_VOLUME));
         JSet("T", trade_id, JR_TICKET);
         ReportOpenFromJournal(trade_id);
         return;
      }
      JSet("T", trade_id, JR_FAILED);
      SendOpenReport(trade_id, 0, "failed", 0, 0, "order_sent_unconfirmed",
                     0, 0, 0, true);
      Print("STOIC v1.50: ORDER_SENT unconfirmed, refused resend trade=",
            trade_id);
      return;
   }
   JSet("T", trade_id, JR_RECEIVED);

   MqlTradeRequest req; MqlTradeResult res;
   ZeroMemory(req); ZeroMemory(res);

   // v1.29 — Resolve the broker's actual symbol name (handles .x/.raw/pro/etc.)
   string broker_symbol = ResolveBrokerSymbol(symbol);
   if (broker_symbol == "") {
      JSet("T", trade_id, JR_FAILED);
      SendOpenReport(trade_id, 0, "failed", 0, 0,
                     "symbol_not_found:" + symbol, 0, 0, 0, false);
      Print("[v1.29] ExecuteTrade aborted — broker has no symbol matching '", symbol, "' (tried bare + 18 suffixes)");
      return;
   }

   // v1.51 — normalise the lot against the BROKER's volume constraints
   // (min / max / step); a lot below the broker minimum is terminal.
   double vol_step = SymbolInfoDouble(broker_symbol, SYMBOL_VOLUME_STEP);
   double vol_min  = SymbolInfoDouble(broker_symbol, SYMBOL_VOLUME_MIN);
   double vol_max  = SymbolInfoDouble(broker_symbol, SYMBOL_VOLUME_MAX);
   double norm_lot = lot;
   if (vol_step > 0) norm_lot = MathFloor(norm_lot / vol_step + 0.5) * vol_step;
   if (vol_max > 0 && norm_lot > vol_max) norm_lot = vol_max;
   norm_lot = NormalizeDouble(norm_lot, 8);
   if (norm_lot < vol_min || norm_lot <= 0) {
      JSet("T", trade_id, JR_FAILED);
      SendOpenReport(trade_id, 0, "failed", 0, 0,
                     StringFormat("volume_below_broker_min:%.4f<%.4f",
                                  norm_lot, vol_min), 0, 0, 0, false);
      Print("STOIC v1.51: lot ", lot, " below broker minimum ", vol_min,
            " on ", broker_symbol, " — terminal reject");
      return;
   }

   req.action       = TRADE_ACTION_DEAL;
   req.symbol       = broker_symbol;
   req.volume       = norm_lot;
   req.deviation    = Slippage;
   req.magic        = MagicNumber;
   req.comment      = trade_id;   // v1.50 — crash-window recovery key
   req.type_filling = PickFillingMode(broker_symbol);

   double price = (action == "BUY") ? SymbolInfoDouble(broker_symbol, SYMBOL_ASK)
                                    : SymbolInfoDouble(broker_symbol, SYMBOL_BID);
   req.price = price;
   int side = (action == "BUY") ? 1 : -1;
   // v1.38 — clamp to broker stop rules + normalise with the TRADED
   // symbol's digits (was chart _Digits, wrong when EA chart != symbol).
   double adj_sl = sl, adj_tp = tp;
   ClampStops(broker_symbol, side, adj_sl, adj_tp, 1);
   req.sl    = adj_sl;
   req.tp    = adj_tp;
   req.type  = (action == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;

   // v1.50 — broker-native preflight: MT5 itself validates margin, volume
   // limits, stop/freeze levels and fill policy BEFORE submission. A failed
   // check reports a structured rejection and never touches the broker.
   MqlTradeCheckResult chk;
   ZeroMemory(chk);
   if (!OrderCheck(req, chk)) {
      JSet("T", trade_id, JR_FAILED);
      string chk_comment = chk.comment;
      StringReplace(chk_comment, "\"", "'");   // keep the JSON body valid
      string perr = StringFormat("preflight_failed:retcode=%d:%s",
                                 chk.retcode, chk_comment);
      SendOpenReport(trade_id, 0, "failed", 0, req.price, perr,
                     sl, adj_sl, 0, false);
      Print("STOIC v1.50: OrderCheck preflight REJECTED ", broker_symbol,
            " retcode=", chk.retcode, " comment=", chk.comment,
            " margin=", chk.margin, " free=", chk.margin_free);
      return;
   }

   JSet("T", trade_id, JR_ORDER_SENT);   // v1.50 — journal BEFORE OrderSend
   bool ok = OrderSend(req, res);

   // v1.38 — if the broker still says INVALID_STOPS (price moved between
   // clamp and send, or stricter internal rules), refresh the price and
   // retry ONCE with a doubled safety buffer. Never opens without SL.
   if (!(ok && res.retcode == TRADE_RETCODE_DONE) && res.retcode == TRADE_RETCODE_INVALID_STOPS) {
      Print("[v1.38] retcode 10016 INVALID_STOPS on ", broker_symbol,
            " sl=", adj_sl, " tp=", adj_tp, " — retrying with widened stops");
      adj_sl = sl; adj_tp = tp;
      ClampStops(broker_symbol, side, adj_sl, adj_tp, 2);
      req.price = (action == "BUY") ? SymbolInfoDouble(broker_symbol, SYMBOL_ASK)
                                    : SymbolInfoDouble(broker_symbol, SYMBOL_BID);
      req.sl = adj_sl;
      req.tp = adj_tp;
      ZeroMemory(res);
      // v1.50 — the retry runs under the SAME broker-native preflight
      // guarantees as the first attempt.
      ZeroMemory(chk);
      if (!OrderCheck(req, chk)) {
         JSet("T", trade_id, JR_FAILED);
         string chk_comment2 = chk.comment;
         StringReplace(chk_comment2, "\"", "'");
         SendOpenReport(trade_id, 0, "failed", 0, req.price,
                        StringFormat("preflight_failed_retry:retcode=%d:%s",
                                     chk.retcode, chk_comment2),
                        sl, adj_sl, 0, false);
         return;
      }
      ok = OrderSend(req, res);
   }

   // v1.52 — verify the ACTUAL broker outcome, never the retcode alone.
   // DONE_PARTIAL means a REAL position exists for the filled part; a
   // failed retcode may still have executed (requote/timeout edges) — the
   // deal → position resolution below is the final authority.
   bool accepted = (ok && (res.retcode == TRADE_RETCODE_DONE
                        || res.retcode == TRADE_RETCODE_DONE_PARTIAL));
   ulong deal_tk = res.deal;
   ulong pos_id  = PositionIdFromDeal(deal_tk);
   if (pos_id == 0)
      pos_id = FindPositionForTrade(trade_id, broker_symbol, action,
                                    req.volume);
   // v1.53 — brokers can lag writing the deal to history; retry briefly
   // before declaring the accepted order unresolved.
   for (int rtry = 0; rtry < 3 && accepted && pos_id == 0; rtry++) {
      Sleep(300);
      pos_id = PositionIdFromDeal(deal_tk);
      if (pos_id == 0)
         pos_id = FindPositionForTrade(trade_id, broker_symbol, action,
                                       req.volume);
   }

   // v1.53 — the trade's TRUE fill comes from its own DEALS, never from
   // POSITION_VOLUME (netting: total symbol position != this trade's fill).
   double filled = OrderFilledVolume(res.order);
   if (filled <= 0 && res.volume > 0) filled = res.volume;
   double pos_volume = 0;
   if (pos_id > 0 && SelectPositionById(pos_id, broker_symbol))
      pos_volume = PositionGetDouble(POSITION_VOLUME);
   // v1.53 — partial-fill truth = deal volume vs requested within half a
   // volume step; DONE_PARTIAL is diagnostic only.
   double half_step = (vol_step > 0 ? vol_step : 0.01) / 2.0;
   bool partial = (filled > 0 && filled + half_step < req.volume);

   // v1.53 — BROKER_ACCEPTED_UNRESOLVED: an accepted order without a
   // resolved position identifier must NEVER be reported open (an order
   // ticket is not a position ticket). The trade stays pending — the risk
   // reservation stays active — and redispatch re-runs resolution.
   if (accepted && pos_id == 0) {
      JSetTicket("K", trade_id, res.order);
      JSetTicket("D", trade_id, deal_tk);
      JSet("T", trade_id, JR_ACCEPTED);
      SendOpenReport(trade_id, 0, "pending", res.price, req.price,
                     "accepted_unresolved", sl, adj_sl, 0, false,
                     res.order, deal_tk, 0, filled, partial, 0);
      Print("STOIC v1.53: accepted but UNRESOLVED trade=", trade_id,
            " order=", res.order, " deal=", deal_tk,
            " — kept pending for re-resolution");
      return;
   }

   bool opened = (pos_id > 0);
   string status = opened ? "open" : "failed";
   string err = opened ? (partial ? "partial_fill" : "")
                       : "retcode=" + IntegerToString(res.retcode);

   // journal EXACT 64-bit order / deal / position tickets, then ack with
   // the ACTUAL broker stop, per-trade fill and netted position volume.
   double conf_sl = 0;
   if (opened) {
      JSetTicket("K", trade_id, res.order);
      JSetTicket("D", trade_id, deal_tk);
      JSetTicket("Q", trade_id, pos_id);
      JSet("P", trade_id, res.price);
      JSet("V", trade_id, filled);
      JSet("T", trade_id, JR_TICKET);
      if (SelectPositionById(pos_id, broker_symbol))
         conf_sl = PositionGetDouble(POSITION_SL);
      if (partial)
         Print("STOIC v1.53: PARTIAL FILL trade=", trade_id, " requested=",
               req.volume, " filled=", filled, " pos_volume=", pos_volume,
               " pos_id=", pos_id);
   } else {
      JSet("T", trade_id, JR_FAILED);
   }
   SendOpenReport(trade_id, pos_id, status, res.price, req.price, err,
                  sl, adj_sl, conf_sl, false,
                  res.order, deal_tk, pos_id, filled, partial, pos_volume);
   if (opened) JSet("T", trade_id, JR_ACK_SENT);
}

// ----- v1.40: FULL_CLOSE — close the entire position by ticket -----
// Fired by the server's modification queue for slippage veto,
// auto-deleverage and reconciler force-closes. Acks via modification-ack;
// the resulting broker "out" deal lands through OnTradeTransaction /
// external-deal with the exact realized P&L.
void ApplyFullClose(string trade_id, long ticket, string intent = "", long seq = 0) {
   if (IsEodQuietWindow()) return;   // v1.41 — deferred, server re-dispatches
   if (!PositionSelectByTicket(ticket)) {
      // Position already gone on the broker — ack success so the queue clears.
      string gone = StringFormat(
         "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"FULL_CLOSE\",\"success\":true,\"intent_id\":\"%s\",\"error\":\"already_closed\"}",
         EffectiveToken, trade_id, intent);
      HttpPost(g_server_url + "/api/bridge/modification-ack", gone);
      MarkIntentDone(intent, seq, trade_id);
      return;
   }
   string symbol = PositionGetString(POSITION_SYMBOL);
   double vol    = PositionGetDouble(POSITION_VOLUME);
   ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);

   MqlTradeRequest req; MqlTradeResult res;
   ZeroMemory(req); ZeroMemory(res);
   req.action    = TRADE_ACTION_DEAL;
   req.symbol    = symbol;
   req.volume    = vol;
   req.deviation = Slippage;
   req.magic     = MagicNumber;
   req.position  = ticket;
   req.type_filling = PickFillingMode(symbol);
   if (type == POSITION_TYPE_BUY) {
      req.type  = ORDER_TYPE_SELL;
      req.price = SymbolInfoDouble(symbol, SYMBOL_BID);
   } else {
      req.type  = ORDER_TYPE_BUY;
      req.price = SymbolInfoDouble(symbol, SYMBOL_ASK);
   }
   // v1.51 — broker-native preflight; a failed check acks retryable so
   // the server re-dispatches instead of burning a broker request.
   string perr = "";
   if (!PreflightOk(req, perr)) {
      string pf = StringFormat(
         "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"FULL_CLOSE\",\"success\":false,\"retryable\":true,\"intent_id\":\"%s\",\"error\":\"%s\"}",
         EffectiveToken, trade_id, intent, perr);
      HttpPost(g_server_url + "/api/bridge/modification-ack", pf);
      return;
   }
   bool ok = OrderSend(req, res);
   // v1.50 — the FINAL truth is the broker position, not the retcode:
   // DONE_PARTIAL (or any failure) with volume remaining is NOT a full
   // close — ack retryable so the server re-dispatches the remainder.
   bool absent = !PositionSelectByTicket(ticket);
   bool success = absent;
   string err = "";
   if (!success) {
      if (ok && res.retcode == TRADE_RETCODE_DONE_PARTIAL)
         err = "partial_close_remaining";
      else
         err = "retcode=" + IntegerToString(res.retcode);
   }
   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"FULL_CLOSE\",\"success\":%s,%s\"intent_id\":\"%s\",\"error\":\"%s\"}",
      EffectiveToken, trade_id, (success ? "true" : "false"),
      (success ? "" : "\"retryable\":true,"), intent, err);
   HttpPost(g_server_url + "/api/bridge/modification-ack", body);
   if (success) MarkIntentDone(intent, seq, trade_id);
   if (success) Print("STOIC: FULL_CLOSE executed ticket=", ticket, " (", vol, " lots)");
}

// v1.50 — server 'close_requested' path, now FENCED through the same
// journal protocol: a deterministic close intent per trade prevents a
// second OrderSend across redispatches, the result is journaled for
// replay, and 'closed' is only reported once the broker position is
// VERIFIED absent (never from the retcode alone).
void ClosePosition(string trade_id, long ticket) {
   if (IsEodQuietWindow()) return;   // v1.41 — deferred, server re-dispatches
   string intent = "close-" + trade_id;
   if (IntentDone(intent)) {
      // executed before but the report was lost — replay journaled result
      string rbody = StringFormat(
         "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"status\":\"closed\",\"exit_price\":%.5f,\"pnl\":%.2f,\"replay\":true}",
         EffectiveToken, trade_id, JGet("X", trade_id), JGet("L", trade_id));
      HttpPost(g_server_url + "/api/bridge/report", rbody);
      return;
   }
   if (!PositionSelectByTicket(ticket)) return;   // heartbeat/ghost reconciler owns absent-position truth
   string symbol = PositionGetString(POSITION_SYMBOL);
   double vol    = PositionGetDouble(POSITION_VOLUME);
   double pnl    = PositionGetDouble(POSITION_PROFIT);   // capture BEFORE close
   ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);

   MqlTradeRequest req; MqlTradeResult res;
   ZeroMemory(req); ZeroMemory(res);
   req.action    = TRADE_ACTION_DEAL;
   req.symbol    = symbol;
   req.volume    = vol;
   req.deviation = Slippage;
   req.magic     = MagicNumber;
   req.position  = ticket;
   req.type_filling = PickFillingMode(symbol);
   if (type == POSITION_TYPE_BUY) {
      req.type  = ORDER_TYPE_SELL;
      req.price = SymbolInfoDouble(symbol, SYMBOL_BID);
   } else {
      req.type  = ORDER_TYPE_BUY;
      req.price = SymbolInfoDouble(symbol, SYMBOL_ASK);
   }
   // v1.51 — broker-native preflight; on failure just return — the server
   // keeps re-dispatching close_requested until the position is verified gone.
   string perr = "";
   if (!PreflightOk(req, perr)) {
      Print("STOIC v1.51: close preflight failed trade=", trade_id, " ", perr);
      return;
   }
   bool ok = OrderSend(req, res);
   // v1.50 — report closed ONLY when the broker position is verified gone.
   if (PositionSelectByTicket(ticket)) {
      Print("STOIC v1.50: close attempt did not remove position ticket=",
            ticket, " ok=", ok, " retcode=", res.retcode,
            " — server will re-dispatch");
      return;
   }
   JSet("X", trade_id, res.price);
   JSet("L", trade_id, pnl);
   MarkIntentDone(intent, 0, trade_id);
   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"status\":\"closed\",\"exit_price\":%.5f,\"pnl\":%.2f}",
      EffectiveToken, trade_id, res.price, pnl);
   HttpPost(g_server_url + "/api/bridge/report", body);
}

// ----- v1.10: SL/TP modify -----
void ApplyModifySL(string trade_id, long ticket, double new_sl,
                   string intent = "", long seq = 0) {
   if (IsEodQuietWindow()) return;   // v1.41 — deferred, server re-dispatches
   if (!PositionSelectByTicket(ticket)) {
      // v1.50 — terminal vs retryable, decided from broker history
      AckMissingPosition(trade_id, "MODIFY_SL", ticket, intent);
      return;
   }
   string symbol = PositionGetString(POSITION_SYMBOL);
   double current_tp = PositionGetDouble(POSITION_TP);

   MqlTradeRequest req; MqlTradeResult res;
   ZeroMemory(req); ZeroMemory(res);
   req.action   = TRADE_ACTION_SLTP;
   req.position = ticket;
   req.symbol   = symbol;
   // v1.38 — clamp the SL move to the broker's stop rules (trailing /
   // breakeven moves near price also trigger retcode 10016 otherwise).
   // TP is passed through untouched.
   long ptype = PositionGetInteger(POSITION_TYPE);
   int  side  = (ptype == POSITION_TYPE_BUY) ? 1 : -1;
   double adj_sl = new_sl, tp_ignore = 0;
   ClampStops(symbol, side, adj_sl, tp_ignore, 1);
   req.sl       = adj_sl;
   req.tp       = current_tp;

   // v1.51 — broker-native preflight (stop/freeze levels re-validated by
   // MT5); a failed check acks success=false so the intent stays retryable.
   string perr = "";
   if (!PreflightOk(req, perr)) {
      string pf = StringFormat(
         "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"MODIFY_SL\",\"success\":false,"
         "\"new_sl\":%.5f,\"requested_sl\":%.5f,\"applied_sl\":%.5f,"
         "\"confirmed_position_sl\":%.5f,\"stop_confirmed\":false,"
         "\"intent_id\":\"%s\",\"error\":\"%s\"}",
         EffectiveToken, trade_id, adj_sl, new_sl, adj_sl,
         PositionGetDouble(POSITION_SL), intent, perr);
      HttpPost(g_server_url + "/api/bridge/modification-ack", pf);
      return;
   }

   bool ok = OrderSend(req, res);
   bool success = (ok && (res.retcode == TRADE_RETCODE_DONE || res.retcode == TRADE_RETCODE_DONE_PARTIAL));
   string err = success ? "" : "retcode=" + IntegerToString(res.retcode);

   // v1.50 — ack the ACTUAL broker stop, not the requested one, and
   // verify the position now SHOWS the expected stop (tick tolerance) —
   // broker-accepted-request is not the same as stop-in-place.
   double confirmed_sl = 0;
   if (PositionSelectByTicket(ticket))
      confirmed_sl = PositionGetDouble(POSITION_SL);
   double tick_sz = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   bool stop_confirmed = (confirmed_sl > 0 &&
                          MathAbs(confirmed_sl - adj_sl) <= tick_sz + 1e-10);
   double ack_sl = (success && confirmed_sl > 0) ? confirmed_sl : adj_sl;

   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"MODIFY_SL\",\"success\":%s,"
      "\"new_sl\":%.5f,\"requested_sl\":%.5f,\"applied_sl\":%.5f,"
      "\"confirmed_position_sl\":%.5f,\"stop_confirmed\":%s,"
      "\"intent_id\":\"%s\",\"error\":\"%s\"}",
      EffectiveToken, trade_id, (success ? "true" : "false"),
      ack_sl, new_sl, adj_sl, confirmed_sl,
      (stop_confirmed ? "true" : "false"), intent, err);
   HttpPost(g_server_url + "/api/bridge/modification-ack", body);
   // v1.52 — 'request accepted' and 'stop confirmed' are SEPARATE states:
   // the intent is consumed ONLY once the live position shows the stop
   // (tick tolerance). Accepted-but-unconfirmed stays retryable — the
   // server re-dispatches until the broker position proves the stop.
   if (success && stop_confirmed) MarkIntentDone(intent, seq, trade_id);
   if (success) Print("STOIC: SL modified ticket=", ticket, " requested=",
                      new_sl, " applied=", adj_sl, " confirmed=", confirmed_sl,
                      " stop_confirmed=", stop_confirmed);
}

// ----- v1.10: Partial close — close (current_vol - new_vol) lots -----
void ApplyPartialClose(string trade_id, long ticket, double new_vol,
                       string intent = "", long seq = 0) {
   if (IsEodQuietWindow()) return;   // v1.41 — deferred, server re-dispatches
   if (!PositionSelectByTicket(ticket)) {
      // v1.50 — terminal vs retryable, decided from broker history
      AckMissingPosition(trade_id, "PARTIAL_CLOSE", ticket, intent);
      return;
   }
   string symbol = PositionGetString(POSITION_SYMBOL);
   double current_vol = PositionGetDouble(POSITION_VOLUME);
   double close_vol = current_vol - new_vol;
   // v1.50 — normalise against the BROKER's volume constraints, not a
   // hardcoded 2-decimal lot step.
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   double minv = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   if (step > 0) close_vol = MathFloor(close_vol / step + 0.5) * step;
   close_vol = NormalizeDouble(close_vol, 8);
   if (close_vol < minv || close_vol <= 0) {
      // nothing executable at this broker's volume rules — terminal
      string tiny = StringFormat(
         "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"PARTIAL_CLOSE\",\"success\":false,\"terminal\":true,\"intent_id\":\"%s\",\"error\":\"close_volume_below_min\"}",
         EffectiveToken, trade_id, intent);
      HttpPost(g_server_url + "/api/bridge/modification-ack", tiny);
      MarkIntentDone(intent, seq, trade_id, 2);
      return;
   }

   ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);

   MqlTradeRequest req; MqlTradeResult res;
   ZeroMemory(req); ZeroMemory(res);
   req.action    = TRADE_ACTION_DEAL;
   req.symbol    = symbol;
   req.volume    = close_vol;
   req.deviation = Slippage;
   req.magic     = MagicNumber;
   req.position  = ticket;
   req.type_filling = PickFillingMode(symbol);
   if (type == POSITION_TYPE_BUY) {
      req.type  = ORDER_TYPE_SELL;
      req.price = SymbolInfoDouble(symbol, SYMBOL_BID);
   } else {
      req.type  = ORDER_TYPE_BUY;
      req.price = SymbolInfoDouble(symbol, SYMBOL_ASK);
   }

   // v1.51 — broker-native preflight (volume + margin re-validated by
   // MT5); a failed check acks success=false so the intent stays retryable.
   string perr = "";
   if (!PreflightOk(req, perr)) {
      string pf = StringFormat(
         "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"PARTIAL_CLOSE\",\"success\":false,\"intent_id\":\"%s\",\"error\":\"%s\"}",
         EffectiveToken, trade_id, intent, perr);
      HttpPost(g_server_url + "/api/bridge/modification-ack", pf);
      return;
   }

   bool ok = OrderSend(req, res);
   bool accepted = (ok && (res.retcode == TRADE_RETCODE_DONE
                        || res.retcode == TRADE_RETCODE_DONE_PARTIAL));

   // v1.52 — success is judged from the ACTUAL remaining broker volume,
   // not the retcode: |remaining − requested| within the broker volume
   // step. A failed retcode whose remaining already matches (a previous
   // attempt landed) IS success; an accepted retcode that left the wrong
   // volume is NOT.
   double remaining = 0;
   if (PositionSelectByTicket(ticket))
      remaining = PositionGetDouble(POSITION_VOLUME);
   double step_tol = (step > 0 ? step : 0.01) + 1e-9;
   bool success = (remaining > 0 && MathAbs(remaining - new_vol) <= step_tol);
   string err = success ? "" :
      (accepted ? "volume_mismatch_after_partial_close"
                : "retcode=" + IntegerToString(res.retcode));

   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"PARTIAL_CLOSE\",\"success\":%s,"
      "\"new_volume\":%.2f,\"remaining_volume\":%.2f,\"intent_id\":\"%s\",\"error\":\"%s\"}",
      EffectiveToken, trade_id, (success ? "true" : "false"),
      new_vol, remaining, intent, err);
   HttpPost(g_server_url + "/api/bridge/modification-ack", body);
   // v1.50 — only broker-confirmed success consumes the intent
   if (success) MarkIntentDone(intent, seq, trade_id);
   if (success) Print("STOIC: Partial close ticket=", ticket, " closed=", close_vol, " remaining=", remaining);
}
