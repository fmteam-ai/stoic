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
//+------------------------------------------------------------------+
#property copyright "STOIC AI Trading"
#property version   "1.20"
#property strict

input string ServerUrl   = "https://your-app.preview.emergentagent.com";
input string BridgeToken = "PASTE_YOUR_BRIDGE_TOKEN_HERE";
input int    PollSeconds = 5;
input int    Slippage    = 10;
input int    MagicNumber = 901234;

datetime lastPoll = 0;

//+------------------------------------------------------------------+
int OnInit() {
   EventSetTimer(PollSeconds);
   Print("STOIC Bridge EA v1.10 started. Polling: ", ServerUrl);
   SendHeartbeat();
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTimer() {
   SendHeartbeat();
   PollPendingTrades();
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

void SendHeartbeat() {
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
   int    openPos = PositionsTotal();
   string body = StringFormat("{\"bridge_token\":\"%s\",\"balance\":%.2f,\"equity\":%.2f,\"open_positions\":%d}",
                              BridgeToken, balance, equity, openPos);
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

   req.action       = TRADE_ACTION_DEAL;
   req.symbol       = symbol;
   req.volume       = NormalizeDouble(lot, 2);
   req.deviation    = Slippage;
   req.magic        = MagicNumber;
   req.type_filling = ORDER_FILLING_IOC;

   double price = (action == "BUY") ? SymbolInfoDouble(symbol, SYMBOL_ASK)
                                    : SymbolInfoDouble(symbol, SYMBOL_BID);
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
   req.type_filling = ORDER_FILLING_IOC;
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
   req.type_filling = ORDER_FILLING_IOC;
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
