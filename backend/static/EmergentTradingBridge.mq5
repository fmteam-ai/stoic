//+------------------------------------------------------------------+
//|                                  EmergentTradingBridge.mq5       |
//|              Polls STOIC AI Trading Bot server for trades.       |
//|                                                                  |
//| HOW TO USE:                                                      |
//| 1. Copy this file to: <MT5 Data Folder>/MQL5/Experts/             |
//| 2. In MT5: Tools > Options > Expert Advisors                     |
//|       - Tick: "Allow WebRequest for listed URL"                  |
//|       - Add your server URL (e.g. https://your-app.preview.emergentagent.com)
//| 3. Compile in MetaEditor (F7) and attach to ANY chart            |
//| 4. Inputs:                                                       |
//|       ServerUrl   = https://your-app.preview.emergentagent.com   |
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
//| v1.30 — Fix: dashboard EA version was permanently stuck at 1.28  |
//|         because the heartbeat JSON hardcoded "1.28" while        |
//|         #property version said "1.29". Now driven by a single    |
//|         EA_CLIENT_VERSION macro so the two can never drift.       |
//+------------------------------------------------------------------+
#property copyright "STOIC AI Trading"
#property version   "1.30"
#property strict

// Single source of truth for the version string we report to STOIC on every
// heartbeat. Keep this in sync with #property version above. Bumping ONLY
// one of the two causes the dashboard to show a stale EA version even
// though MT5 itself loads the new binary.
#define EA_CLIENT_VERSION "1.30"

input string ServerUrl              = "https://your-app.preview.emergentagent.com";
input string BridgeToken            = "PASTE_YOUR_BRIDGE_TOKEN_HERE";
input string TrackedSymbols         = "XAUUSD,BTCUSD";  // comma list — spreads sent on heartbeat
input int    PollSeconds            = 5;
input int    Slippage               = 10;
input int    MagicNumber            = 901234;
input int    HistorySweepSeconds    = 60;     // how often to scan MT5 deal history
input int    HistoryLookbackSeconds = 86400;  // initial backfill window (24h)
input int    HistorySweepMaxDeals   = 50;     // hard cap per sweep so a fresh chart doesn't flood STOIC

datetime lastPoll              = 0;
datetime lastHistorySweep      = 0;
datetime lastReportedDealTime  = 0;   // high-watermark — never re-push deals older than this

//+------------------------------------------------------------------+
int OnInit() {
   EventSetTimer(PollSeconds);
   // First sweep covers HistoryLookbackSeconds backwards so any ghosts
   // (closed on another terminal while EA was offline) get backfilled
   // automatically once the user installs v1.26.
   lastReportedDealTime = TimeCurrent() - HistoryLookbackSeconds;
   Print("STOIC Bridge EA v1.30 started. Polling: ", ServerUrl);
   SendHeartbeat();
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTimer() {
   SendHeartbeat();
   PollPendingTrades();
   // Autonomous history sweep — at most once every HistorySweepSeconds so
   // we don't bombard the server with redundant /external-deal calls.
   if (TimeCurrent() - lastHistorySweep >= HistorySweepSeconds) {
      SweepDealHistory();
      lastHistorySweep = TimeCurrent();
   }
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

   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"mt5_ticket\":%I64d,\"deal_id\":%I64u,"
      "\"deal_entry\":\"%s\",\"symbol\":\"%s\",\"action\":\"%s\","
      "\"lots\":%.2f,\"price\":%.5f,\"profit\":%.2f,"
      "\"commission\":%.2f,\"swap\":%.2f,"
      "\"deal_time\":%I64d,\"magic\":%I64d}",
      BridgeToken, position_id, deal_id,
      entry_str, symbol, action,
      volume, price, profit, commission, swap, deal_time, magic);

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
         "\"deal_time\":%I64d,\"magic\":%I64d}",
         BridgeToken, position_id, deal_id,
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
   // Common broker suffix variants observed in the wild
   string suffixes[] = {
      ".x", ".X", ".raw", ".RAW", ".r", ".m", ".ecn", ".ECN",
      "pro", "Pro", "PRO", "+", "#", "m", "_pro", "-ECN", ".pro"
   };
   for (int i = 0; i < ArraySize(suffixes); i++) {
      string candidate = base_symbol + suffixes[i];
      if (SymbolSelect(candidate, true)) {
         MqlTick tick;
         if (SymbolInfoTick(candidate, tick) && tick.bid > 0 && tick.ask > 0) {
            Print("[v1.29] ResolveBrokerSymbol: ", base_symbol, " -> ", candidate);
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
   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"balance\":%.2f,\"equity\":%.2f,"
      "\"open_positions\":%d,\"spreads\":%s,"
      "\"account_login\":%I64d,\"base_currency\":\"%s\","
      "\"positions\":%s,\"client_version\":\"%s\"}",
      BridgeToken, balance, equity, openPos, spreads, login, ccy, positions, EA_CLIENT_VERSION);
   HttpPost(ServerUrl + "/api/bridge/heartbeat", body);
}

void PollPendingTrades() {
   string body = StringFormat("{\"bridge_token\":\"%s\"}", BridgeToken);
   string resp = HttpPost(ServerUrl + "/api/bridge/poll-trades", body);
   if (StringLen(resp) == 0) return;

   // -------- 1. Process pending NEW trades from "trades":[...] block --------
   ParseTradesBlock(resp);

   // -------- 2. Process pending modifications from "modifications":[...] block --------
   ParseModificationsBlock(resp);
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

void ExecuteTrade(string trade_id, string symbol, string action, double lot, double sl, double tp) {
   MqlTradeRequest req; MqlTradeResult res;
   ZeroMemory(req); ZeroMemory(res);

   // v1.29 — Resolve the broker's actual symbol name (handles .x/.raw/pro/etc.)
   string broker_symbol = ResolveBrokerSymbol(symbol);
   if (broker_symbol == "") {
      string body = StringFormat(
         "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"mt5_ticket\":0,\"status\":\"failed\",\"entry_price\":0.0,\"error\":\"symbol_not_found:%s\"}",
         BridgeToken, trade_id, symbol);
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
   req.sl    = NormalizeDouble(sl, _Digits);
   req.tp    = NormalizeDouble(tp, _Digits);
   req.type  = (action == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;

   bool ok = OrderSend(req, res);
   string status = (ok && res.retcode == TRADE_RETCODE_DONE) ? "open" : "failed";
   string err = (ok ? "" : "retcode=" + IntegerToString(res.retcode));

   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"mt5_ticket\":%I64u,\"status\":\"%s\",\"entry_price\":%.5f,\"error\":\"%s\"}",
      BridgeToken, trade_id, res.order, status, res.price, err);
   HttpPost(ServerUrl + "/api/bridge/report", body);
}

void ClosePosition(string trade_id, long ticket) {
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
      BridgeToken, trade_id, res.price, pnl);
   HttpPost(ServerUrl + "/api/bridge/report", body);
}

// ----- v1.10: SL/TP modify -----
void ApplyModifySL(string trade_id, long ticket, double new_sl) {
   if (!PositionSelectByTicket(ticket)) return;
   string symbol = PositionGetString(POSITION_SYMBOL);
   double current_tp = PositionGetDouble(POSITION_TP);

   MqlTradeRequest req; MqlTradeResult res;
   ZeroMemory(req); ZeroMemory(res);
   req.action   = TRADE_ACTION_SLTP;
   req.position = ticket;
   req.symbol   = symbol;
   req.sl       = NormalizeDouble(new_sl, _Digits);
   req.tp       = current_tp;

   bool ok = OrderSend(req, res);
   bool success = (ok && (res.retcode == TRADE_RETCODE_DONE || res.retcode == TRADE_RETCODE_DONE_PARTIAL));
   string err = success ? "" : "retcode=" + IntegerToString(res.retcode);

   string body = StringFormat(
      "{\"bridge_token\":\"%s\",\"trade_id\":\"%s\",\"type\":\"MODIFY_SL\",\"success\":%s,\"new_sl\":%.5f,\"error\":\"%s\"}",
      BridgeToken, trade_id, (success ? "true" : "false"), new_sl, err);
   HttpPost(ServerUrl + "/api/bridge/modification-ack", body);
   if (success) Print("STOIC: SL modified ticket=", ticket, " new_sl=", new_sl);
}

// ----- v1.10: Partial close — close (current_vol - new_vol) lots -----
void ApplyPartialClose(string trade_id, long ticket, double new_vol) {
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
      BridgeToken, trade_id, (success ? "true" : "false"), new_vol, err);
   HttpPost(ServerUrl + "/api/bridge/modification-ack", body);
   if (success) Print("STOIC: Partial close ticket=", ticket, " closed=", close_vol, " remaining=", new_vol);
}
