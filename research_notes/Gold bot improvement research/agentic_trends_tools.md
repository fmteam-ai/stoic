# Agentic AI Trading Trends and Tooling (2025-2026) for a Retail Gold Bot Using Claude

Evidence grading used below: **[A]** primary source (paper, regulator, vendor press release/repo); **[B]** reputable secondary (law firm, trade press); **[C]** aggregator/blog/marketing, treat as unverified. Note: WebFetch failed (DNS) for arxiv.org, barchart.com and interactivebrokers.co.uk during this session, so several items rely on search-result snippets of primary sources rather than full-text reads.

## Q1. Multi-agent trading frameworks (TradingAgents, FinRobot, FinAgent, ai-hedge-fund): published performance, and do they work intraday?

### Takeaway
The well-known open-source multi-agent frameworks are research/educational systems that use daily data and short backtests. None has independently verified live results, and none is built or tested for intraday gold trading. Their useful part is the architecture (analyst roles, a risk manager, a portfolio-manager or supervisor). Their return claims should not be relied on.

### Cited Findings
- TradingAgents (UCLA/MIT/Tauric Research, arXiv 2412.20138, AAAI 2025 workshop oral). It simulates a trading firm with fundamental, sentiment and technical analyst agents, bull/bear researchers, traders with different risk profiles, and a risk team. It claims better cumulative return, Sharpe ratio and max drawdown than Buy&Hold, MACD, KDJ/RSI, SMA and ZMR baselines [A] — [arXiv](https://arxiv.org/pdf/2412.20138); [HF papers](https://huggingface.co/papers/2412.20138)
- The TradingAgents backtest covered only Jan 1 to Mar 29, 2024. Reported Sharpe above 8 for AAPL, and the paper itself says this may be an artifact of the short period. Live deployment is listed as future work [A/B] — [arXiv v6](https://arxiv.org/abs/2412.20138v6); [DigitalOcean guide](https://www.digitalocean.com/resources/articles/tradingagents-llm-framework)
- virattt/ai-hedge-fund has roughly 46.7k stars and 8.1k forks (Mar 8, 2026 snapshot); other sources report 50k to 59k. Agents are modeled on famous investors (Buffett, Graham, Wood and others), plus sentiment, fundamentals, technicals, valuation, risk-manager and portfolio-manager agents. It is stated to be for educational and research purposes only, not live trading [C] — [OSS Metrics](https://oosmetrics.com/repo/virattt/ai-hedge-fund); [PyShine](https://pyshine.com/AI-Hedge-Fund-Multi-Agent-Investment-System/); [SourcePulse](https://www.sourcepulse.org/projects/1797841)
- InvestorBench (ACL 2025) describes FinRobot as built for market analysis, and FinMem/FinAgent as oriented to trading single equities/ETFs. It says these frameworks "generally focus on addressing only limited types of financial decision-making tasks" [A] — [InvestorBench, ACL 2025](https://preview.aclanthology.org/ingestion-acl-25/2025.acl-long.126.pdf)
- Agent Market Arena (arXiv 2510.11695) ran four agent frameworks live (InvestorAgent, TradeAgent, HedgeFundAgent, DeepFundAgent). Finding: the framework design (behavior and risk style) drove outcome differences more than the choice of LLM backbone. Assets were TSLA, BMRN, BTC and ETH, paper-traded with transaction costs not included [A/C] — [arXiv](https://arxiv.org/abs/2510.11695); [AI Street interview](https://aistreet.beehiiv.com/p/benchmarking-ai-agents-in-live-markets)

### Inferences
- For the gold bot, the most transferable pattern is role separation: news analyst, then a risk agent with veto power, then deterministic execution. Copying the full trading-firm debate loops is less useful, because they are slow and costly for intraday cycles.
- The AMA finding that frameworks matter more than models suggests effort is better spent on risk rules and position-sizing logic than on switching models.
- No framework above was tested on XAUUSD or at intraday horizons. Any intraday use would be novel and needs its own walk-forward validation.

### Gaps
- No independent, audited live P&L was found for TradingAgents, FinRobot, FinAgent or ai-hedge-fund.
- No published intraday (minute/hour bar) evaluation of these frameworks was found.
- No gold- or FX-specific multi-agent LLM study was found in this search.

## Q2. Benchmarks of LLMs trading live or semi-live (Alpha Arena/nof1, StockBench, InvestorBench, LiveTradeBench, FinBen): what they showed

### Takeaway
Live and long-horizon benchmarks from late 2025 to 2026 point the same way. Most frontier LLMs fail to beat buy-and-hold or lose money when trading autonomously. Results are high-variance and sensitive to prompts and setup, and general benchmark rankings do not predict trading skill. Claude Sonnet 4.5 lost about 31% in Alpha Arena Season 1 (crypto).

### Cited Findings
- **Alpha Arena Season 1** (nof1, Oct 17/18 to about Nov 3-5, 2025; dates differ by source). Six models each got $10k of real money to trade crypto perpetuals on Hyperliquid with no human intervention. Qwen3 Max won at about +22% ($12,231; 30.2% win rate over 43 trades), DeepSeek was about +4.9%, Claude Sonnet 4.5 about -30.8%, Grok 4 -45.3%, Gemini 2.5 Pro -56.7%, GPT-5 -62.7%. Four of six lost money [B/C] — [Forklog](https://forklog.com/en/four-out-of-six-ai-models-suffer-losses-in-trading-tournament/); [GNcrypto](https://www.gncrypto.news/news/qwen-wins-alpha-arena-season-1-with-22-percent-returns/)
- nof1 founder Jay Azhang said the models showed consistent biases ("personalities") that persisted across prompt variations, and acknowledged limited context and a constrained action space [C] — [GNcrypto](https://www.gncrypto.news/news/qwen-wins-alpha-arena-season-1-with-22-percent-returns/); [Towards AI critique, "the leaderboard lied"](https://pub.towardsai.net/six-frontier-ai-models-traded-60-000-in-real-money-for-17-days-the-leaderboard-lied-to-everyone-9f73591a8d4b)
- **Alpha Arena Season 1.5** moved to US equities, with four themed contests (New Baseline, Monk Mode, Situational Awareness, Max Leverage). A "mystery model" later identified as Grok 4.20 averaged about +12% and was profitable in all four. GPT-5.1 placed second and Gemini 3 third. Forklog's reporting could not be cross-checked against nof1's own leaderboard [C] — [Forklog](https://forklog.com/en/ai-model-grok-4-2-triumphs-in-trading-tournament/)
- **StockBench** (Tsinghua/BUPT, arXiv 2510.02209, v2 Mar 2026). Multi-month daily trading on DJIA stocks using prices, fundamentals and news, tested on GPT-5, Claude-4, Qwen3, Kimi-K2, GLM-4.5 and others. Most agents fail to beat buy-and-hold, and the authors say agents "rarely outperform simple baselines." It excludes HFT/intraday and multi-asset [A] — [arXiv](https://arxiv.org/abs/2510.02209v1); [code](https://github.com/ChenYXxxx/stockbench)
- **InvestorBench** (ACL 2025). 13 LLM backbones across stocks, crypto and ETFs. Larger and proprietary models do better, but all models struggle in highly volatile conditions [A] — [arXiv](https://arxiv.org/html/2412.18174v1)
- **LiveTradeBench** (arXiv 2511.03628). Tested 21 LLMs live. Models that top LMArena do not show top trading performance [A] — [arXiv](https://arxiv.org/html/2511.03628v1)
- **TradeTrap** (arXiv 2512.02261) found LLM trading agents fragile under perturbation (see Q5) [A] — [arXiv](https://arxiv.org/pdf/2512.02261)

### Inferences
- Live evidence does not support giving Claude, or any LLM, discretionary end-to-end trading authority. The safer role is a bounded signal or filter (news classification, event risk flags) inside deterministic risk and execution code.
- Alpha Arena's short windows (about 2.5 weeks) and few trades (for example 43 for the winner) are statistically weak. Rankings are close to noise, so they should not be used to pick a model.
- Volatility sensitivity (InvestorBench) matters for gold, which has news-driven spikes around CPI, NFP and FOMC. The LLM layer is weakest exactly when the stakes are highest.

### Gaps
- FinBen-specific results were not retrieved in this search.
- No Alpha Arena season was found that trades gold or FX.
- nof1's primary leaderboard was not fetched, and Season 1 end dates conflict across sources.
- No 2026 Alpha Arena Season 2 results were found.

## Q3. MCP servers, connectors and tools a bot owner could plug in (market data, backtesting, brokers, monitoring)

### Takeaway
The MCP ecosystem for market data is mature, with official servers from Massive/Polygon, Twelve Data, Alpha Vantage, FMP and Anthropic's financial-services partners. Broker connectivity split during 2026. IBKR launched an official MCP connector (Jul 28, 2026), while MT5 and cTrader rely on community servers of uneven quality. For a live gold bot, MCP is best used for research, monitoring and supervision. It should not sit on the low-latency order path.

### Cited Findings
**Market data**
- Massive.com (formerly Polygon.io): official MCP server in the polygon-io GitHub org, MIT license. It exposes three composable tools (search, call, query) covering the full API, installs via `claude mcp add`, and is flagged experimental with possible breaking changes. `POLYGON_API_KEY` is deprecated in favor of `MASSIVE_API_KEY` [A] — [Glama README](https://glama.ai/mcp/servers/@polygon-io/mcp_polygon/blob/7d4dd00bdafba4cd0054ad988b19d0c1fa6e682b/README.md); [mcpservers.org](https://mcpservers.org/servers/polygon-io/mcp_polygon)
- Twelve Data: official open-source MCP server for stocks, forex, crypto, ETFs and commodities (time series, quotes, RSI/MACD/BBands/ATR/ADX). Runs locally or remotely via uvx, pip or Docker; MIT. Listed as early-stage, and endpoint access depends on plan [A] — [Twelve Data docs](https://twelvedata.com/docs/llms/ai/mcp-server.md); [LobeHub](https://lobehub.com/zh/mcp/twelvedata-mcp)
- Anthropic's Claude for Financial Services (launched about Jul 15, 2025) ships prebuilt MCP connectors to FactSet, S&P Capital IQ, MSCI, PitchBook, Morningstar, LSEG, Daloopa and others, later adding Excel integration and agent skills. MCP was donated to the Linux Foundation's Agentic AI Foundation in Dec 2025 [B] — [VentureBeat](https://venturebeat.com/ai/financial-firms-get-a-purpose-built-claude-as-anthropic-bets-on-vertical-ai-platforms); [Celent, May 2026](https://www.celent.com/en/insights/anthropic-launches-software-for-financial-services-may-2026)
- Observed first-hand in this session's tool list: an Alpha Vantage MCP server (including `GOLD_SILVER_SPOT`, `GOLD_SILVER_HISTORY`, `FX_INTRADAY`, `NEWS_SENTIMENT`, `CPI`, `NONFARM_PAYROLL`, `FEDERAL_FUNDS_RATE`, `TREASURY_YIELD`, ATR/RSI indicators), an FMP MCP (`commodity`, `forex`, `news`, `calendar`, `economics`, `commitmentOfTraders`, `technicalIndicators`), a Bigdata.com MCP (news search, sentiment, events calendar) and a TextToQuant backtesting MCP (`run_backtest`, `start_analysis` walk-forward/sweeps, `get_overfit_verdict`, `get_regime_edge`, `create_scan_alert`, webhooks) [A, observed in session connectors, not web-verified]

**Brokers**
- Interactive Brokers press release, Jul 28, 2026: "Opens AI Connectivity to Any Tool Built on the MCP Standard". Clients add the IBKR connector URL as a custom MCP connector, log in through IBKR's secure screen, and authorize a single account. It works across Client Portal, IBKR Desktop, GlobalTrader, Mobile and TWS [A, search snippet] — [IBKR UK press release](https://www.interactivebrokers.co.uk/en/general/about/mediaRelations/7-28-26.php); [IBKR IE](https://interactivebrokers.ie/en/general/about/mediaRelations/7-28-26.php)
- An earlier report (around Jun 1, 2026) describes an IBKR-Claude integration giving access to positions, orders, margin, history and market data [C] — [aiagentstore](https://aiagentstore.ai/ai-agent-news/topic/trading/2026-06-09)
- Sources conflict on whether AI can execute orders directly through IBKR's connector. One aggregator says order primitives are exposed after consent; StockBrokers.com says execution stays outside the AI session and the user submits the order [C] — [aiagentstore](https://aiagentstore.ai/ai-agent-news/topic/trading/2026-06-09/detailed); contradicted by [StockBrokers.com](https://www.stockbrokers.com/guides/ai-agent-brokers)
- Community IBKR MCP servers exist (for example code-rabi/interactive-brokers-mcp), self-described as unofficial, not endorsed and alpha-state [A] — [Glama](https://glama.ai/mcp/servers/@code-rabi/interactive-brokers-mcp/blob/dc2095c53552b7379015dbba7a6c8ebc858032e7/README.md)
- MetaTrader 5, all community-built:
  - Qoyyuum/mcp-metatrader5-server (PyPI; about 128 stars and 51 forks; last push Apr 2026) covers market data, order placement/management and history. It needs Python 3.11+ and a Windows MT5 terminal, and supports stdio or HTTP.
  - amirkhonov/metatrader5-mcp (MIT, 32 tools).
  - ariadng/metatrader-mcp-server (adds a WebSocket tick server and REST). One listing says `get_balance` returns demo/random values, so verify before use [B/C] — [gittrend](https://gittrend.io/repo/Qoyyuum/mcp-metatrader5-server); [Algolia docsearch](https://docsearch.algolia.com/mcp/docs/repo/ariadng/metatrader-mcp-server); [Glama](https://glama.ai/mcp/servers/@sameerasulakshana/mcpmt5)

### Inferences
- A practical stack:
  1. Keep the MT5 Python bridge or EA as the deterministic execution path.
  2. Add a read-only MT5 MCP, or a wrapped one with order tools disabled, for supervisor agents to inspect positions and logs.
  3. Use Alpha Vantage/FMP/Twelve Data MCPs for gold spot, macro calendar, COT and news-sentiment cross-checks.
  4. Use a backtesting MCP (such as TextToQuant) for walk-forward and overfit checks on strategy changes proposed by agents.
- Community MT5 MCP servers that expose `order_send` to an LLM are high-risk. Fork, pin a version, strip trading tools, and run them against a demo account first.
- For IBKR users, the official connector with single-account OAuth authorization is the most production-grade broker MCP found. Its order-execution scope needs confirming in IBKR docs.

### Gaps
- No official Databento, TradingView, cTrader or Financial Datasets MCP server could be confirmed in these searches. Each needs a dedicated check.
- No dedicated trading monitoring/alerting MCP (for example Grafana or PagerDuty in a trading context) was researched. Generic options exist, but none was verified here.
- The IBKR connector's exact tool list and execution permissions remain unconfirmed (primary page fetch failed).

## Q4. 2026 trends: tokenized gold, 24/7 markets, CME micro/weekly gold options, AI execution, regulation

### Takeaway
2026 brought round-the-clock gold trading on CME: 1-Ounce Gold futures went 24/7 from Jul 24, 2026, joining existing weekend on-chain tokenized gold. Weekly micro gold options give retail-sized hedging. Brokers are opening AI/MCP connectivity (IBKR), and regulators are folding AI into existing algo-trading rules rather than writing new ones.

### Cited Findings
- CME Group: nearly 15,000 1-Ounce Gold futures (about $60M notional) traded in the first weekend of the new 24/7 schedule (press release, Jul 27, 2026) [A, via snippet] — [Barchart/PRNewswire](https://www.barchart.com/story/news/3484644/traders-drive-strong-demand-in-first-weekend-of-cme-group-s-24-7-gold-futures)
- The 24/7 schedule for the 1OZ contract started Friday, Jul 24, 2026. Brokers may not support weekend trading, and GTC/GTD orders on 1OZ now expire weekly [A, broker notice] — [Phillip Nova](https://www.phillipnova.com.sg/announcement/important-notice-weekly-expiration-of-cme-groups-1-ounce-gold-futures-gtc-gtd-orders/)
- Options on Micro Gold futures have weekly expiries on the nearest four Mondays, Wednesdays and Fridays, trading nearly 24 hours Sunday to Friday on Globex [A] — [CME education](https://www.cmegroup.com/education/courses/understanding-micro-futures-contracts-at-cme-group/micro-gold-and-silver-futures/understanding-options-on-micro-gold-futures.html)
- Tokenized gold trades on-chain over weekends, and some institutions watch it as a price-discovery signal for CME gap risk ahead of the Sunday reopen [B] — [Cointelegraph](https://cointelegraph.com/news/tokenized-gold-weekend-price-discovery-cme-closed?rand=50490)
- IBKR opened MCP connectivity to any MCP tool (Jul 28, 2026), and Anthropic expanded its financial-services connectors and agent skills through 2026 (see Q3) [A/B] — [IBKR](https://interactivebrokers.ie/en/general/about/mediaRelations/7-28-26.php); [Celent](https://www.celent.com/en/insights/anthropic-launches-software-for-financial-services-may-2026)
- ESMA's supervisory briefing on algorithmic trading (Feb 26, 2026) covers AI, pre-trade controls, governance, stress testing and outsourcing (see Q5) [B] — [Macfarlanes](https://www.macfarlanes.com/insights/102mpep/algorithmic-trading-and-artificial-intelligence-esma-supervisory-briefing/)

### Inferences
- For a retail XAUUSD CFD bot on MT5, weekend 24/7 futures and tokenized gold create a new data source. Weekend CME 1OZ or tokenized-gold prices can estimate the Sunday/Monday gap before the MT5 market reopens, which can drive pre-open risk decisions such as cutting exposure ahead of a likely gap.
- Weekly micro gold options could hedge event risk (FOMC/NFP weeks), but CFD-only MT5 brokers will not offer them. That would need a futures broker such as IBKR.

### Gaps
- No CME-issued tokenized gold product was found.
- It is unconfirmed whether Micro Gold options or 10oz/100oz contracts also moved to 24/7.
- No hard data was found on adoption of "AI execution" products for retail FX/CFD.

## Q5. Risks and regulation: prompt injection from news, model drift, EU AI Act/ESMA/FCA, broker/prop-firm EA rules; security best practices

### Takeaway
Feeding untrusted news to an LLM inside a trading loop is a documented attack surface. 2025-2026 papers show hidden-text headlines, misinformation and prompt injection measurably degrade returns without changing trading frequency, so the damage is hard to see. Regulation applies existing MiFID II algo/RTS 6 and conduct rules to AI. The EU AI Act's high-risk deadlines were pushed to Dec 2027 and do not clearly cover trading. For a retail owner, broker and prop-firm terms are the binding constraint.

### Cited Findings
**Attacks and robustness**
- TradeTrap (arXiv 2512.02261, Dec 2025): a "reverse-expectation" prompt-injection attack cut one adaptive agent's return from 7.81% to 0.89% and Sharpe from 5.72 to 0.29, while trading frequency stayed near baseline. Injection degrades decision quality rather than execution, so ordinary activity monitoring may not catch it [A, snippet] — [arXiv](https://arxiv.org/pdf/2512.02261)
- "Adversarial News and Lost Profits" (arXiv 2601.13082, Jan 2026) manipulated headlines against an LLM trading system, including hidden `display:none` HTML text that humans do not see but the model reads, flipping predicted sentiment [A, snippet] — [arXiv](https://arxiv.org/html/2601.13082v1)
- An ICLR 2026 study of multi-agent trading systems tested role-specific attacks: data poisoning and indirect prompt injection on analyst agents, and objective hijacking on trader agents [A] — [ICLR 2026](https://iclr.cc/virtual/2026/10016542)
- AutoRedTrader (arXiv 2605.09185, May 2026): synthetic financial misinformation achieved attack success up to 26.67% even against grounded agents [A, snippet] — [EmergentMind](https://www.emergentmind.com/papers/2605.09185)
- An agentic-commerce SoK (arXiv 2604.15367) classes financial prompt injection by vector: data feeds, on-chain metadata, and inter-agent communication [A] — [arXiv](https://arxiv.org/pdf/2604.15367)
- Model "personality" biases persisted across prompt variations in Alpha Arena, implying a model swap or version change can shift behavior materially [C] — [GNcrypto](https://www.gncrypto.news/news/qwen-wins-alpha-arena-season-1-with-22-percent-returns/)

**Regulation**
- ESMA statement (May 30, 2024): firms using AI for retail investment services must still meet MiFID II organisational, conduct and best-interest duties. Flagged risks are bias, opacity, overreliance and data privacy/security [A] — [ESMA](https://esma.europa.eu/press-news/esma-news/esma-provides-guidance-firms-using-artificial-intelligence-investment-services)
- ESMA supervisory briefing on algorithmic trading (Feb 26, 2026): covers pre-trade controls, governance, stress testing, outsourcing and AI. Firms should consider AI's influence in annual RTS 6 self-assessments. It is non-binding with no comply-or-explain [B] — [Macfarlanes](https://www.macfarlanes.com/insights/102mpep/algorithmic-trading-and-artificial-intelligence-esma-supervisory-briefing/); [Linklaters](https://financialregulation.linklaters.com/post/102mknu/mifid-ii-esma-issues-supervisory-briefing-on-algorithmic-trading); [A&L Goodbody](https://www.algoodbody.com/insights-publications/asset-management-investment-funds-eu-international-developments-march-2026)
- FCA, Aug 21, 2025: multi-firm review of algorithmic trading controls (RTS 6) at principal trading firms, covering governance, self-assessment/validation, the role of compliance and algo inventories. The FCA AI Live Testing scheme (with Advai) focuses on retail services such as advice and debt, not trading. Cohort 2 opened Jan 2026 with testing from April [A/B] — [FCA](https://www.fca.org.uk/news/press-releases/fca-helps-firms-test-ai-safely); [IFA Magazine](https://ifamagazine.com/fca-helps-firms-to-test-ai-safely/)
- EU AI Act Digital Omnibus: published in the OJ on Jul 24, 2026 and in force Jul 27, 2026. Annex III high-risk obligations (credit scoring, hiring and others) are deferred to Dec 2, 2027, and Annex I embedded systems to Aug 2, 2028. Article 50 transparency and GPAI obligations still applied from Aug 2, 2026 [B] — [Gibson Dunn](https://www.gibsondunn.com/eu-ai-act-omnibus-agreement-postponed-high-risk-deadlines-and-other-key-changes/); [Software Improvement Group](https://www.softwareimprovementgroup.com/eu-ai-act-summary/)

**Broker and prop-firm rules**
- Prop firms have generally moved from banning bots to banning behaviors: latency arbitrage, tick scalping, signal copying, martingale/grid (contested), news-window trades, and identical trade signatures across accounts. MT5 is the standard EA platform, while some firms ban EAs on cTrader, Match-Trader or DXtrade. FXIFY and FundingPips are reported to ban EAs on instant/competition accounts. Some firms allow AI only for research and prohibit fully automated AI bots. These sources are mostly affiliate blogs [C] — [PropFirmsFinder](https://propfirmsfinder.com/guides/eas-and-automated-trading-prop-firms/); [PineConnector on FTMO](https://www.pineconnector.com/blogs/pico-blog/ftmo-challenge-rules-automated-traders); [Alpha Capital Group](https://alphacapitalgroup.uk/resources/can-you-use-ai-or-chatgpt-to-pass-a-prop-firm-evaluation-2026); [PropFirmMap](https://propfirmmap.com/blog/prop-firm-trading-rules-explained-eas-news-copy-trading-2026)

### Inferences (security best practices for an LLM reading untrusted news in a trading loop; derived from the attacks above, not from a single authoritative guide)
1. **Strip and normalise input.** Remove HTML, CSS-hidden text, zero-width characters and markup before Claude sees a headline. This counters the `display:none` attack directly.
2. **Constrain output.** The news model should return only a strict JSON schema (for example `{event_type, direction, confidence, source_ids}`), validated in code. Never let it return free-text instructions or tool calls.
3. **Least privilege.** The news-reading model has no tools: no order or file tools and no MCP broker access. Supervisor agents get read-only broker access. Order placement stays in deterministic code with hard caps on lot size, daily loss, maximum open positions and a news-window blackout.
4. **Cross-source corroboration.** Require two or more independent reputable sources, or a price/volatility confirmation, before a news signal can change exposure. This blunts single-feed poisoning (AutoRedTrader, data-feed injection).
5. **Bound the LLM's influence.** Let the LLM veto, reduce or flag rather than increase size. An injected "bullish" cue then cannot cause outsized losses.
6. **Monitor decision quality, not just activity.** TradeTrap showed injection keeps trading frequency normal. Track per-signal hit rate and P&L attribution of LLM-influenced trades, and alert on drift.
7. **Pin and canary model versions.** Pin the exact Claude model ID and keep a fixed evaluation set of historical headlines with known labels. Re-run it on any model upgrade, and alert if classification agreement drops. This addresses model drift and personality bias.
8. **Log for audit.** Keep full logs (raw input, sanitised input, model output, decision) and a kill switch. This also matches ESMA/FCA expectations on governance, testing and algo inventories, even though a private retail trader is not directly an RTS 6 firm.
9. **Treat inter-agent messages as untrusted data,** especially supervisor-to-executor channels. This follows the SoK vector of inter-agent communication.

### Gaps
- No ESMA/FCA/SEC/CFTC guidance aimed specifically at individual retail traders running their own AI bots was found. Existing rules mainly bind investment firms and brokers.
- No SEC/CFTC 2025-2026 publications were retrieved in this search.
- Whether autonomous trading AI falls in any EU AI Act Annex III category was not confirmed, though it appears unlikely to be high-risk by default.
- Specific retail MT5 CFD broker terms on AI EAs (as opposed to prop firms) were not researched.
- Full texts of the attack papers were not read (fetch failures), so the quantified figures come from abstracts/snippets.
