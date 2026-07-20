//+------------------------------------------------------------------+
//|                                  EmergentTradingBridge.mq5       |
//|              Polls STOIC AI Trading Bot server for trades.       |
//|                                                                  |
//| HOW TO USE:                                                      |
//| 1. Copy this file to: <MT5 Data Folder>/MQL5/Experts/             |
//| 2. In MT5: Tools > Options > Expert Advisors                     |
//|       - Tick: "Allow WebRequest for listed URL"                  |
//|       - Add your server URL (e.g. https://algo-trade-135.preview.emergentagent.com)
//| 3. Compile in MetaEditor (F7) and attach to ANY chart            |
//| 4. Inputs:                                                       |
//|       ServerUrl   = https://algo-trade-135.preview.emergentagent.com   |
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
//+------------------------------------------------------------------+
#property copyright "STOIC AI Trading"
#property version   "1.48"
#property strict

// Single source of truth for the version string we report to STOIC on every
// heartbeat. Keep this in sync with #property version above. Bumping ONLY
// one of the two causes the dashboard to show a stale EA version even
// though MT5 itself loads the new binary.
#define EA_CLIENT_VERSION "1.48"

input string ServerUrl              = "https://algo-trade-135.preview.emergentagent.com";
input string BridgeToken            = "PASTE_YOUR_BRIDGE_TOKEN_HERE";
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
   // EA v1.36: resolve token from inputs OR auto-installer drop file.
   EffectiveToken = ResolveBridgeToken();
   // EA v1.43: subscribe to the broker's order book (no-op if unsupported).
   if (DomEnabled) _dom_subscribed = MarketBookAdd(_Symbol);
   Print("STOIC Bridge EA v", EA_CLIENT_VERSION, " started. Polling: ", ServerUrl);
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
   HttpPost(ServerUrl + "/api/bridge/candles", body);
}

void SendCandles() {
   string sent = "," + _Symbol + ",";
   SendCandlesFor(_Symbol);
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
   HttpPost(ServerUrl + "/api/bridge/dom", body);
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

   HttpPost(ServerUrl + "/api/bridge/external-deal", body);
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

      HttpPost(ServerUrl + "/api/bridge/external-deal", body);
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
   HttpPost(ServerUrl + "/api/bridge/ticks", body);
}

//+------------------------------------------------------------------+
string HttpPost(string url, string body) {
   char post[]; char result[]; string headers;
   StringToCharArray(body, post, 0, StringLen(body), CP_UTF8);
   string req_headers = "Content-Type: application/json\r\n";
   ResetLastError();
   int res = WebRequest("POST", url, req_headers, 10000, post, result, headers);
   if (res == -1) {
      Print("WebRequest error: ", GetLastError(), " (Add ", url, " to allowed URLs)");
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
void AppendSymbolSpec(string &json, string sym, bool &first) {
   if (sym == "" || StringFind(json, "\"" + sym + "\":") >= 0) return;
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);
   if (point <= 0) return;
   long digits  = SymbolInfoInteger(sym, SYMBOL_DIGITS);
   long stops   = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   long freeze  = SymbolInfoInteger(sym, SYMBOL_TRADE_FREEZE_LEVEL);
   if (!first) json += ",";
   json += StringFormat(
      "\"%s\":{\"point\":%.8f,\"digits\":%I64d,"
      "\"stops_level_points\":%I64d,\"freeze_level_points\":%I64d}",
      sym, point, digits, stops, freeze);
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
   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"balance\":%.2f,\"equity\":%.2f,"
      "\"open_positions\":%d,\"spreads\":%s,"
      "\"account_login\":%I64d,\"base_currency\":\"%s\","
      "\"positions\":%s,\"client_version\":\"%s\","
      "\"symbol_specs\":%s,"
      "\"available_symbols\":%s}",
      EffectiveToken, balance, equity, openPos, spreads, login, ccy, positions,
      EA_CLIENT_VERSION, BuildSymbolSpecsJson(), CACHED_AVAILABLE_SYMBOLS);
   HttpPost(ServerUrl + "/api/bridge/heartbeat", body);
}

void PollPendingTrades() {
   // v1.41 — skip the poll entirely during the EOD quiet window so no
   // open/modify/close is even fetched; the server re-dispatches after.
   if (IsEodQuietWindow()) return;
   string body = StringFormat("{\"bridge_token\":\"%s\"}", EffectiveToken);
   string resp = HttpPost(ServerUrl + "/api/bridge/poll-trades", body);
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
   HttpPost(ServerUrl + "/api/bridge/external-deal", body);
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
   HttpPost(ServerUrl + "/api/bridge/sync-complete", body);
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

      if (close_req && ticket > 0) {
         ClosePosition(trade_id, ticket);
      } else if (ticket == 0) {
         ExecuteTrade(trade_id, symbol, action, lot, sl, tp);
      }

      idx = brace_pos + 1;
      if (idx <= 0) break;
   }
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

      string mod_type = ExtractString(section, "\"type\":\"", t_end);
      long ticket = (long)ExtractDouble(section, "\"mt5_ticket\":", t_end);
      double new_sl = ExtractDouble(section, "\"new_sl\":", t_end);
      double new_vol = ExtractDouble(section, "\"new_volume\":", t_end);

      int brace_pos = StringFind(section, "}", t_end);

      if (mod_type == "MODIFY_SL" && ticket > 0 && new_sl > 0) {
         ApplyModifySL(trade_id, ticket, new_sl);
      } else if (mod_type == "PARTIAL_CLOSE" && ticket > 0 && new_vol > 0) {
         ApplyPartialClose(trade_id, ticket, new_vol);
         // Combo: Tier-1 move also carries new_sl
         if (new_sl > 0) ApplyModifySL(trade_id, ticket, new_sl);
      } else if (mod_type == "FULL_CLOSE" && ticket > 0) {
         // v1.40 — slippage veto / auto-deleverage / reconciler force-close.
         // Previous builds ignored this type entirely (stuck-queue bug).
         ApplyFullClose(trade_id, ticket);
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

void ExecuteTrade(string trade_id, string symbol, string action, double lot, double sl, double tp) {
   if (IsEodQuietWindow()) return;   // v1.41 — deferred, server re-dispatches
   MqlTradeRequest req; MqlTradeResult res;
   ZeroMemory(req); ZeroMemory(res);

   // v1.29 — Resolve the broker's actual symbol name (handles .x/.raw/pro/etc.)
   string broker_symbol = ResolveBrokerSymbol(symbol);
   if (broker_symbol == "") {
      string body = StringFormat(
         "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"mt5_ticket\":0,\"status\":\"failed\",\"entry_price\":0.0,\"error\":\"symbol_not_found:%s\"}",
         EffectiveToken, trade_id, symbol);
      HttpPost(ServerUrl + "/api/bridge/report", body);
      Print("[v1.29] ExecuteTrade aborted — broker has no symbol matching '", symbol, "' (tried bare + 18 suffixes)");
      return;
   }

   req.action       = TRADE_ACTION_DEAL;
   req.symbol       = broker_symbol;
   req.volume       = NormalizeDouble(lot, 2);
   req.deviation    = Slippage;
   req.magic        = MagicNumber;
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
      ok = OrderSend(req, res);
   }

   string status = (ok && res.retcode == TRADE_RETCODE_DONE) ? "open" : "failed";
   string err = (status == "open") ? "" : "retcode=" + IntegerToString(res.retcode);

   // v1.40 — include the price we ASKED for so the server can measure true
   // broker slippage (fill vs request) instead of signal-to-fill drift.
   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"mt5_ticket\":%I64u,\"status\":\"%s\",\"entry_price\":%.5f,\"requested_price\":%.5f,\"error\":\"%s\"}",
      EffectiveToken, trade_id, res.order, status, res.price, req.price, err);
   HttpPost(ServerUrl + "/api/bridge/report", body);
}

// ----- v1.40: FULL_CLOSE — close the entire position by ticket -----
// Fired by the server's modification queue for slippage veto,
// auto-deleverage and reconciler force-closes. Acks via modification-ack;
// the resulting broker "out" deal lands through OnTradeTransaction /
// external-deal with the exact realized P&L.
void ApplyFullClose(string trade_id, long ticket) {
   if (IsEodQuietWindow()) return;   // v1.41 — deferred, server re-dispatches
   if (!PositionSelectByTicket(ticket)) {
      // Position already gone on the broker — ack success so the queue clears.
      string gone = StringFormat(
         "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"FULL_CLOSE\",\"success\":true,\"error\":\"already_closed\"}",
         EffectiveToken, trade_id);
      HttpPost(ServerUrl + "/api/bridge/modification-ack", gone);
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
   bool ok = OrderSend(req, res);
   bool success = (ok && res.retcode == TRADE_RETCODE_DONE);
   string err = success ? "" : "retcode=" + IntegerToString(res.retcode);
   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"FULL_CLOSE\",\"success\":%s,\"error\":\"%s\"}",
      EffectiveToken, trade_id, (success ? "true" : "false"), err);
   HttpPost(ServerUrl + "/api/bridge/modification-ack", body);
   if (success) Print("STOIC: FULL_CLOSE executed ticket=", ticket, " (", vol, " lots)");
}

void ClosePosition(string trade_id, long ticket) {
   if (IsEodQuietWindow()) return;   // v1.41 — deferred, server re-dispatches
   if (!PositionSelectByTicket(ticket)) return;
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
   bool ok = OrderSend(req, res);
   double pnl = PositionGetDouble(POSITION_PROFIT);
   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"status\":\"closed\",\"exit_price\":%.5f,\"pnl\":%.2f}",
      EffectiveToken, trade_id, res.price, pnl);
   HttpPost(ServerUrl + "/api/bridge/report", body);
}

// ----- v1.10: SL/TP modify -----
void ApplyModifySL(string trade_id, long ticket, double new_sl) {
   if (IsEodQuietWindow()) return;   // v1.41 — deferred, server re-dispatches
   if (!PositionSelectByTicket(ticket)) return;
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

   bool ok = OrderSend(req, res);
   bool success = (ok && (res.retcode == TRADE_RETCODE_DONE || res.retcode == TRADE_RETCODE_DONE_PARTIAL));
   string err = success ? "" : "retcode=" + IntegerToString(res.retcode);

   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"MODIFY_SL\",\"success\":%s,\"new_sl\":%.5f,\"error\":\"%s\"}",
      EffectiveToken, trade_id, (success ? "true" : "false"), new_sl, err);
   HttpPost(ServerUrl + "/api/bridge/modification-ack", body);
   if (success) Print("STOIC: SL modified ticket=", ticket, " new_sl=", new_sl);
}

// ----- v1.10: Partial close — close (current_vol - new_vol) lots -----
void ApplyPartialClose(string trade_id, long ticket, double new_vol) {
   if (IsEodQuietWindow()) return;   // v1.41 — deferred, server re-dispatches
   if (!PositionSelectByTicket(ticket)) return;
   string symbol = PositionGetString(POSITION_SYMBOL);
   double current_vol = PositionGetDouble(POSITION_VOLUME);
   double close_vol = current_vol - new_vol;
   if (close_vol < 0.01) return;
   close_vol = NormalizeDouble(close_vol, 2);

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

   bool ok = OrderSend(req, res);
   bool success = (ok && res.retcode == TRADE_RETCODE_DONE);
   string err = success ? "" : "retcode=" + IntegerToString(res.retcode);

   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"PARTIAL_CLOSE\",\"success\":%s,\"new_volume\":%.2f,\"error\":\"%s\"}",
      EffectiveToken, trade_id, (success ? "true" : "false"), new_vol, err);
   HttpPost(ServerUrl + "/api/bridge/modification-ack", body);
   if (success) Print("STOIC: Partial close ticket=", ticket, " closed=", close_vol, " remaining=", new_vol);
}
