# Data Sources and APIs to Improve a Retail XAUUSD Trading Bot (2025-2026)

Research date: 2026-10-17. Many vendor pricing pages could not be fetched directly (databento.com failed DNS from the research sandbox), so several prices come from search-result snippets or third-party aggregators. Each one is flagged. Confirm on the vendor page before budgeting.

MCP connectors already attached to this Claude session (seen in its tool list, not in web research): **Alpha Vantage MCP** (GOLD_SILVER_SPOT/HISTORY, NEWS_SENTIMENT, TREASURY_YIELD, FEDERAL_FUNDS_RATE, CPI, NONFARM_PAYROLL, FX_INTRADAY), **FMP MCP** (commitmentOfTraders, economics, calendar, news, commodity, forex, etfAndMutualFunds) and **Bigdata.com MCP** (news/document search, events calendar, sentiment tearsheets). These are the lowest-effort additions for an LLM-driven bot.

## Gold futures (COMEX GC/MGC) tick, volume, order-flow and depth data, and whether it helps spot/CFD gold

### Takeaway
Real centralized volume and depth for gold comes from COMEX futures. Retail-affordable routes range from about $11-16/month in exchange fees (IBKR or Rithmic, non-pro Level 2) up to about $199/month (Databento Standard or Massive Advanced). Academic work generally finds that COMEX futures lead London spot in price discovery, so futures order flow and volume profile are a reasonable proxy signal for XAUUSD CFDs. Leadership does shift over time, though, and the CFD feed carries a basis/spread offset.

### Cited Findings
- **Databento**: the Standard plan for CME launched in April 2025 at $179/month, and usage-based pricing for live CME data was discontinued on 2025-04-16. A 2026-05-29 announcement says CME Globex MDP 3.0 monthly rates move to $199 / $1,750 / $4,500 from 2026-06-22. Existing Standard subscribers keep $179 for 12 months. — [Databento blog: Updates to subscription pricing](https://databento.com/blog/updates-to-subscription-pricing); [Introducing new CME pricing plans](https://databento.com/blog/introducing-new-cme-pricing-plans)
- Databento tiers are Usage-based, Standard, Plus and Unlimited. A third-party listing gives the earlier prices: Standard $179/month, Plus $1,500/month and Unlimited $4,000/month (annual contract for the latter two). This is consistent with the 2026 rise to $199 / $1,750 / $4,500. Historical pay-as-you-go is "from $0.50/GB". — [databento.com/pricing](https://databento.com/pricing); [FitGap listing](https://us.fitgap.com/products/033314/databento); [databento.com](https://databento.com/)
- A user-submitted roadmap post says L2/L3 (MBP-10/MBO) were pulled from the Standard plan, which suggests full depth needs the higher tiers. This is unconfirmed. — [Databento roadmap](https://roadmap.databento.com/b/n0o5prm6/feature-ideas/eurex-eobi-dataset)
- An older Databento announcement said live CME data started at $32.65/month for non-professionals and $740/month for professional non-display. This is likely superseded. — [Databento roadmap announcement](https://roadmap.databento.com/announcements/live-cme-data-is-now-open-to-all-users-starting-at-3265month)
- **Massive (formerly Polygon.io)**: futures APIs became generally available on 2026-05-28. They cover real-time and historical data for ES, GC and CL from CME/CBOT/COMEX/NYMEX over REST, WebSocket and flat files. — [Massive blog: Futures data has arrived](https://www.massive.com/blog/futures-data-has-arrived)
- Massive tiers (secondhand, stated as "similar" for futures): Basic $0, Starter $29/month, Developer $79/month, Advanced $199/month (real-time), Business $2,000/month (commercial). Each asset class is billed separately. — [edgeful comparison 2026](https://www.edgeful.com/blog/posts/futures-data-api-polygon-databento-edgeful-comparison); [apicostcalc on the rebrand](https://apicostcalc.com/it/blog/polygon-massive-rebrand-api-pricing.html)
- **Interactive Brokers**: the fee table lists COMEX Real-Time Depth of Book (L2) at USD 11.00 / USD 135.00 (most likely non-pro / pro) and COMEX Top of Book at USD 1.25. A third-party summary cites 2026 CME non-pro rates of $1.55/month (top of book) and $12.10/month (depth) per exchange. The sources conflict slightly. — [IBKR market data fees](https://www.interactivebrokers.ie/en/index.php?f=37645); [NexusFi exchange data fees](https://nexusfi.com/a/data/exchange-data-fees-market-data-costs)
- IBKR offers a "Futures Value PLUS" bundle with Level 2 for CBOT/CME/COMEX/NYMEX. The price was not found. — [IBKR knowledge base](https://ibkb.interactivebrokers.com/fr/node/2840)
- **Rithmic** (via the Ironbeam FCM): a $25/month connection fee plus $0.10 per contract. Non-pro L1 is $3.00 per exchange, non-pro L2 is $15.50 per exchange including COMEX, and pro CME is $140 per exchange. AMP charges a separate Rithmic API (R|API) fee of $100/month per user ID on top of the $25 fee. Rithmic carries the full Globex depth. — [Ironbeam Rithmic API](https://www.ironbeam.com/rithmic-api-futures-trading/); [AMP Futures Rithmic fees](https://faq.ampfutures.com/hc/en-us/articles/360060137993); [Optimus forum](https://community.optimusfutures.com/t/rithmic-api-and-api-differences-and-cost/3274)
- **Price discovery**: Hauptfleisch, Putniņš and Lucey ("Who sets the price of gold? London or New York", J. Futures Markets, intraday data 1997-2014) find New York futures play a larger role in price discovery than London spot, despite much smaller volume. The cited reasons are higher liquidity and lower trading costs. — [Hauptfleisch et al. (UTS repository)](https://opus.lib.uts.edu.au/bitstream/10453/41414/4/GoldILS%20JFutMkt%20Forthcoming.pdf)
- That finding is contradicted by earlier work (Lucey et al. 2013), which named London the dominant centre. Leadership shifts during crises. — [O'Connor et al., London or New York (UCLan)](https://knowledge.lancashire.ac.uk/id/eprint/6563/1/oconnor_6563_London%20or%20New%20York%20%282%29.pdf)
- London OTC is about 78% of global gold turnover versus about 8% for COMEX (2015 estimate, secondary source). — [BullionStar](https://www.bullionstar.com/blogs/bullionstar/what-sets-the-gold-price-is-it-the-paper-market-or-physical-market/)

### Inferences
- The cheapest workable stack for real gold volume and order-flow features (delta, footprint, volume profile, DOM imbalance) is an IBKR account with COMEX L2 (about $11-12/month non-pro) via the TWS API, or Rithmic R|API (about $125/month plus about $15.50 of data). Databento Standard ($199) is the cleanest developer API and comes with history. Massive Advanced (about $199) is a reasonable option for GC bars/ticks, but its futures product is new (May 2026) and its depth coverage is unverified.
- The CFD spot price differs from GC by basis (the carry to the front contract) and roll effects. Signals should be mapped as relative levels (e.g., POC/VWAP offset by the live GC-minus-XAUUSD spread) rather than absolute prices.
- Non-professional status matters. A bot run for personal accounts is usually non-pro. Running it for a business or fund triggers professional fees (about 10x higher).

### Gaps
- Databento's current per-tier schema entitlements (whether MBO/MBP-10 is included in Standard) could not be confirmed because the pricing page failed to load.
- dxFeed retail futures pricing and CME DataMine historical tick pricing for GC were not found.
- No 2025-2026 empirical study was found that directly tests COMEX order-flow features as predictors for retail XAUUSD CFD returns.

## Macro drivers: real yields, DXY, Fed expectations, economic calendar APIs

### Takeaway
The core macro inputs are free or cheap. FRED provides real yields (DFII10) and other series via a free API key. The CME FedWatch API starts at about $25/month. Calendar APIs with actual, forecast and previous values cost about $22-59/month (FMP), about $50+/month (Finnhub, unverified) or about $149-299/month (Trading Economics API, unverified).

### Cited Findings
- **FRED** hosts daily series, for example the Cboe gold vol index GVZCLS, which shows how FRED serves free daily market series. The 10-year TIPS real yield is series DFII10. — [FRED GVZCLS](https://fred.stlouisfed.org/series/GVZCLS); [FRED DFII10](https://fred.stlouisfed.org/series/DFII10)
- **CME FedWatch API**: entry-level pricing "as little as $25/month". It is a REST/JSON API with a 60-second real-time stream and end-of-day snapshots, and needs OAuth API ID entitlement via CME self-service onboarding. Tiered pricing reported by third parties ($25 for 0-1,000 elements, $75 for 1,001-50,000) is unverified. — [CME FedWatch API](https://www.cmegroup.com/market-data/market-data-api/fedwatch-api.html); [CME FedWatch EOD API docs](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457320466/CME+FedWatch+API); [findmymoat comparison](https://www.findmymoat.com/vs/cme-fedwatch-vs-financialdata-net)
- **FMP**: Starter about $22/month (300 calls/min, 5 years of history, US) and Premium about $59/month (750 calls/min, 30 years, US/UK/CA). FMP lists an Economic Data Releases Calendar API, but the tier that unlocks it is unconfirmed. FMP has an MCP server. — [NexusFi FMP listing](https://nexusfi.com/d/data-providers/financial-modeling-prep/); [FMP economics datasets](https://site.financialmodelingprep.com/datasets/economics); [FMP MCP getEconomicCalendar](https://policylayer.com/tools/cfocoder-financial-modeling-prep-mcp-server/geteconomiccalendar)
- **Trading Economics**: the API is priced by features, request volume and distribution. The calendar endpoint supports streaming updates at release time. Standard is about $149/month and Professional about $299/month, both billed yearly (third-party, June 2026). Trials are limited to 100k data points and 100 requests. The separate web subscription is $199/month. — [Trading Economics API](https://www.tradingeconomics.com/api); [apis.io TE plans](https://apis.io/plans/tradingeconomics/tradingeconomics-plans-pricing/); [TE pricing](https://tradingeconomics.com/analytics/pricing.aspx?source=nav)
- **Finnhub**: the free tier is about 60 calls/min, non-commercial. Paid plans are about $50/month starter and about $100-500+/month premium, with add-ons. These are third-party figures, and whether the economic calendar is included is unconfirmed. — [apicostcalc Finnhub](https://apicostcalc.com/de/finnhub.html)
- **Alpha Vantage**: the free tier allows 25 requests/day. Premium plans cost $49.99 / $99.99 / $149.99 / $199.99 / $249.99 per month (75-1,200 req/min). There is an official MCP server (attached here) with TREASURY_YIELD, FEDERAL_FUNDS_RATE, CPI, NONFARM_PAYROLL and FX endpoints. — [apicostcalc Alpha Vantage](https://apicostcalc.com/alpha-vantage.html); [alphalog guide](https://alphalog.ai/blog/alphavantage-api-complete-guide)

### Inferences
- DXY is an ICE index. Real-time ICE DXY futures data needs an ICE feed. A free or cheap proxy is a synthetic DXY computed from EURUSD/USDJPY/GBPUSD/USDCAD/USDSEK/USDCHF, which the CFD broker already provides, using the standard ICE weights.
- For gold, the event-risk value of a calendar comes mostly from timing (blackout windows around CPI/NFP/FOMC) plus surprise (actual minus consensus). FMP Starter/Premium plus its MCP is likely the best value. Trading Economics is better if the bot needs push-on-release latency.
- Forex Factory and Investing.com have no official APIs. Scraping them is fragile and likely against their terms of service.

### Gaps
- Official FMP, Finnhub and Trading Economics price pages could not be verified directly. Tiers for economic-calendar entitlement are unclear.
- FRED API rate limits were not verified in this pass. The key is known to be free.
- No verified source was found for low-latency (sub-second) release-time economic data for retail use.

## Positioning and flows: CFTC COT, gold ETF flows, central bank buying, options/volatility

### Takeaway
COT, GVZ and WGC ETF/central-bank data are all free but low-frequency (weekly, daily or monthly). CME Gold CVOL (GCVL, with skew) needs a paid DataMine licence. Use these as regime and bias filters, not as entry triggers.

### Cited Findings
- **CFTC COT**: data is as of Tuesday and released Friday afternoon. The disaggregated report (since 2009) splits traders into Producer/Merchant, Swap Dealers, Managed Money and Other Reportables. — [CFTC COT descriptions](https://www.cftc.gov/node/128971); [CFTC release 5710-09](https://www.cftc.gov/PressRoom/PressReleases/pr5710-09.html)
- FMP exposes COT through its MCP (commitmentOfTraders tool), which is attached to this session. — [FMP MCP server listing](https://policylayer.com/tools/cfocoder-financial-modeling-prep-mcp-server/geteconomiccalendar)
- **World Gold Council Goldhub**: ETF holdings and flows for more than 100 physically-backed gold ETFs, with regional and fund-level detail in tonnes and USD. Sources are Bloomberg, company filings and ICE Benchmark Administration. Central bank data is offered as XLSX ("Changes in World Official Gold Reserves", plus a quarterly series since 2000). — [Goldhub ETF holdings and flows](https://www.gold.org/goldhub/data/gold-etfs-holdings-and-flows/); [Goldhub data](https://www.gold.org/goldhub/data)
- **GVZ** (Cboe Gold ETF Volatility Index, 30-day implied vol from GLD options, calculated since 2009) is free daily on FRED as GVZCLS. The latest snapshot value was 27.12 on 2026-07-01. — [FRED GVZCLS](https://fred.stlouisfed.org/series/GVZCLS); [Forex.com GVZ guide](https://forex.com/en-us/news-and-analysis/gvz-index/?amp=true)
- **CME Gold CVOL (GCVL)** publishes ATM vol, Skew, Convexity and Up/Down Variance. The EOD index is published daily via CME DataMine with up to 9 years of history, and intraday files run from 2022-08-08 onward. Delivery is by API, SFTP, S3 or email, and licensing fees apply (amounts not published). The GCVL EOD value on 2026-08-17 was 24.9057. — [CME CVOL docs](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457326006/CME+Group+Volatility+Indexes+-+CVOL); [CVOL intraday values](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/46118530/CVOL+Intraday+Values); [CME CVOL notice Aug 2026](https://www.cmegroup.com/market-data/cme-group-benchmark-administration/files/cvol-eod-publication-delay-notice-resolved-17-august-2026.pdf); [Introducing Gold CVOL](https://www.cmegroup.com/news/2021/introducing-the-gold-cvol-index.html)
- Alpha Vantage's MCP also offers options put/call and volume/open-interest ratio tools (REALTIME_PUT_CALL_RATIO, HISTORICAL_OPTIONS) that could be applied to GLD. This is an observation from the session tool list. Premium tiers start at $49.99/month. — [apicostcalc Alpha Vantage](https://apicostcalc.com/alpha-vantage.html)

### Inferences
- Daily GLD/IAU share-count changes, a proxy for ETF flow, can be derived from issuer holdings files or from FMP/Alpha Vantage ETF endpoints for free or cheaply. WGC aggregates are monthly and suit regime context only.
- GVZ or GCVL levels and changes are useful for volatility-scaling position size and stops. GCVL skew (calls versus puts) is the only readily available measure of gold options skew, and it costs money.

### Gaps
- CFTC Socrata/API dataset IDs were not retrieved in this pass. The public reporting environment exists, but the endpoints were not verified.
- CME DataMine fee amounts for CVOL and GC tick history were not published.
- No source was found for an official API for WGC data. Downloads appear to be XLSX only.

## News feeds suitable for LLM reading

### Takeaway
For an LLM gold bot, the cheapest workable feeds are Alpha Vantage NEWS_SENTIMENT ($0-50/month, MCP available), FMP news (MCP) and Finnhub. NewsAPI.org is expensive ($449/month for commercial use) and returns only snippets. Benzinga requires a sales quote. Reuters and Bloomberg feeds are institutional-priced and nothing was found for them.

### Cited Findings
- **NewsAPI.org**: Developer $0 (development only), Business $449/month (250k requests, real-time, 5 years of history) and Advanced $1,749/month (2M requests, 99.95% SLA). Overage is $0.0018 / $0.0009 per request. No plan returns full article text. — [NewsAPI pricing](https://newsapi.org/pricing); [apicostcalc NewsAPI](https://apicostcalc.com/newsapi.html)
- **Benzinga**: the free Basic News API returns headline, teaser and link only. Production API access is offered "for 6 months at an incredibly low rate", with no published price. Benzinga Pro (consumer) is reportedly $99 / $197 / $457 per month, with API access only on the top tier (third-party). — [Benzinga APIs](https://www.benzinga.com/apis/en-ca/?p=3615); [Subger Benzinga Pro](https://subger.com/en/service/benzinga-pro-sub)
- **Alpha Vantage** news and sentiment is part of "Alpha Intelligence" and appears to be included in the standard tiers (free at 25 requests/day, premium $49.99+). This inclusion is inferred and unconfirmed. — [alphalog guide](https://alphalog.ai/blog/alphavantage-api-complete-guide)
- **Finnhub** free tier includes company news. Paid plans are about $50/month and up (third-party). — [apicostcalc Finnhub](https://apicostcalc.com/de/finnhub.html)

### Inferences
- Gold-moving news is mostly macro and geopolitical (Fed speakers, CPI, tariffs, conflict, central-bank buying), not company-specific. Feeds with good macro tagging matter more than equity-news depth. Bigdata.com (MCP attached) and Alpha Vantage topic filters (e.g., "economy_monetary") are the most practical for LLM ingestion.
- For speed, headline feeds (Benzinga Pro squawk, Bloomberg/Reuters terminals) are ahead of REST news APIs by seconds to minutes. A retail bot polling REST news should not try to trade the headline itself. Use news for regime tagging or as a no-trade filter.

### Gaps
- No verified latency figures were found for any news API.
- Reuters (LSEG) and Bloomberg API pricing were not researched or found. These are known to be enterprise contracts.

## Retail sentiment (broker client positioning) and evidence for contrarian use

### Takeaway
IG Client Sentiment (via the IG/DailyFX pages) and the Myfxbook Community Outlook are free but have no official sentiment API. Access is by scraping or third-party actors. Academic evidence confirms retail FX traders behave as contrarians and lose on average. Evidence that fading them is profitable after costs is mixed, and no independent XAUUSD-specific backtest was found.

### Cited Findings
- IG/DailyFX guidance is to fade extreme positioning (60% or more on one side). Its own material warns of false signals in choppy markets and says the tool is not foolproof. This is vendor educational content, not a tested result. — [DailyFX: Trading signals from IG client sentiment](https://dailyfx.com/education/market-sentiment/trading-signals-from-ig-client-sentiment.html); [IG: Trading on sentiment](https://ig.com/sg/trading-strategies/trading-on-sentiment--using-ig-client-sentiment-data-220930)
- Myfxbook Community Outlook shows XAUUSD long/short percentages, lots and position counts. One illustrative snapshot showed 63% long and 37% short. There is no official sentiment API. An Apify scraper and MQL5 tools read the public page, and Myfxbook's documented API covers account data only. — [Myfxbook Community Outlook](https://myfxbook.com/community/outlook); [Apify Myfxbook scraper](https://apify.com/xtracto/myfxbook-community-outlook); [MQL5 sentiment indicator](https://www.mql5.com/de/market/product/80271); [R-bloggers Myfxbook API](https://www.r-bloggers.com/2014/04/r-interface-to-myfxbook/)
- Nolte and Nolte (Eur. J. Finance 2016), using about 2,000 OANDA retail traders, find their trading carries information exploitable for short-term out-of-sample FX forecasting. — [IDEAS/RePEc](https://ideas.repec.org/a/taf/eurjfi/v22y2016i2p80-104.html)
- A UCLan study finds retail FX traders trade contrarian, driven by lagged returns rather than macro news, and that simple cross-over strategies exploiting retail order flow "could be profitable". — [UCLan: News and intraday retail investor order flow in FX](https://clok.uclan.ac.uk/55052)
- A Lithuanian study of dealer long/short ratios finds retail positions lean against the trend, but none of the active strategies built on them beat buy-and-hold. — [eLABa repository](https://talpykla.elaba.lt/elaba-fedora/objects/elaba:3306721/datastreams/MAIN/content)
- CFTC research (Ferko, Mixon, Onur) on futures finds retail traders generally follow contrarian strategies. — [CFTC](https://www.cftc.gov/node/249641)

### Inferences
- Retail sentiment is best used as a weak filter: avoid joining the crowd at extremes, combined with trend. It is not a standalone contrarian trigger. In strong trends (e.g., gold's 2024-2026 bull run), "retail net short" can persist for a long time.
- A bot can build its own history by snapshotting Myfxbook/IG data every 15-60 minutes, then backtest before relying on it. If the CFD broker publishes its own client-positioning stats, that is the most relevant population.

### Gaps
- No independent, peer-reviewed backtest of IG Client Sentiment or Myfxbook outlook on gold/XAUUSD was found.
- Whether IG's REST API (for account holders) exposes client sentiment programmatically was not verified in this pass.
- Myfxbook's terms on scraping were not reviewed.
