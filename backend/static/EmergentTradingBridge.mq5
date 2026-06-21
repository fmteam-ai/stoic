//+------------------------------------------------------------------+
//|                                  EmergentTradingBridge.mq5       |
//|              Polls Emergent AI Trading Bot server for trades.    |
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
//+------------------------------------------------------------------+
#property copyright "Emergent AI Trading Bot"
#property version   "1.00"
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
   Print("Emergent Bridge EA started. Polling: ", ServerUrl);
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

   // Naive JSON parsing — pulls each trade block.
   int idx = 0;
   while (true) {
      int t_start = StringFind(resp, "\"trade_id\":\"", idx);
      if (t_start < 0) break;
      t_start += 12;
      int t_end = StringFind(resp, "\"", t_start);
      string trade_id = StringSubstr(resp, t_start, t_end - t_start);

      string symbol = ExtractString(resp, "\"symbol\":\"", t_end);
      string action = ExtractString(resp, "\"action\":\"", t_end);
      double lot    = ExtractDouble(resp, "\"lot_size\":", t_end);
      double sl     = ExtractDouble(resp, "\"stop_loss\":", t_end);
      double tp     = ExtractDouble(resp, "\"take_profit\":", t_end);
      bool   close_req = (StringFind(resp, "\"close_requested\":true", t_end) > 0 &&
                          StringFind(resp, "\"close_requested\":true", t_end) <
                          StringFind(resp, "}", t_end));
      long ticket   = (long)ExtractDouble(resp, "\"mt5_ticket\":", t_end);

      if (close_req && ticket > 0) {
         ClosePosition(trade_id, ticket);
      } else if (ticket == 0) {
         ExecuteTrade(trade_id, symbol, action, lot, sl, tp);
      }

      idx = StringFind(resp, "}", t_end) + 1;
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
