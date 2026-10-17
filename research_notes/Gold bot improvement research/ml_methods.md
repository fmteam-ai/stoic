# ML/AI Methods for Retail Intraday FX & Gold (XAUUSD) Bots: Evidence vs Hype (2023-2026)

Evidence grades used throughout: STRONG = several independent, peer-reviewed or replicated out-of-sample results net of costs; MIXED = some credible OOS support but contradicted, cost-sensitive, or limited to other asset classes; WEAK = mostly vendor, student, or in-sample claims. Context: a small retail bot with rule-based engines, an LLM news reader, and at most a few thousand trades of history.

Research caveat: arxiv.org, alphaxiv.org and emergentmind.com could not be fetched from this environment (DNS failures), so several 2025-2026 papers below are summarized from search-result abstracts and snippets, not full text. Treat the numbers as abstract-level.

## 1. Meta-labeling and ML trade filters on top of rule-based signals

### Takeaway
Grade: WEAK-to-MIXED. This is the most retail-appropriate ML add-on: a secondary classifier that decides whether to take, and how much to size, a trade the rule engine already signals. The only out-of-sample "evidence" is vendor write-ups and student theses, and practitioners report it often adds nothing. With a few thousand labeled trades it is feasible, but it needs purged/embargoed validation and a very small feature set.

### Cited Findings
- Meta-labeling is credited to López de Prado (2017/2018, *Advances in Financial Machine Learning*). It separates the side decision (the primary model or rules) from the sizing / take-or-skip decision (the secondary ML model). — [Wikipedia: Meta-Labeling](https://en.wikipedia.org/wiki/Meta-Labeling); [UZH thesis](https://www.merlin.uzh.ch/publication/show/22343)
- Joubert (with Hudson & Thames) published follow-ups in the Journal of Financial Data Science: "Meta-Labeling: Calibration and Position Sizing", which treats the secondary model's output as the estimated probability that a trade is profitable, and "Ensemble Meta-Labeling". I saw only the titles and descriptions, not the results. — [Joubert LinkedIn listing](https://ae.linkedin.com/in/jacquesjoubert)
- Practitioner implementations use the predicted probability of the "take the bet" class directly to scale position size. — [Jesse docs: meta-labeling](https://docs.jesse.trade/docs/research/ml/meta-labeling)
- Hudson & Thames (a vendor selling the MLFinLab tooling) reports that event-based sampling plus triple-barrier labels plus meta-labeling improved out-of-sample performance of trend-following and mean-reversion strategies. This is a toy example. — [Hudson & Thames toy example](https://hudsonthames.org/meta-labeling-a-toy-example/); [H&T research update](https://hudsonthames.org/?p=3448)
- A University of Zurich master's thesis found that meta-labeling improved three existing trading systems. — [UZH](https://www.merlin.uzh.ch/publication/show/22343)
- An Insper undergraduate study on a MACD strategy found that backtest metrics improved in every configuration. The ML models themselves "did not perform expressively", and the authors say the method needs testing on a stronger primary system. — [Insper](https://repositorio.insper.edu.br/entities/publication/14261761-ee4f-4466-91e4-771dd445f258/full)
- A QuantConnect forum thread titled "why meta labeling is not a silver bullet" includes a user who got no improvement from adding a meta-model to an existing strategy. — [QuantConnect forum](https://www.quantconnect.com/forum/discussion/14706/why-meta-labeling-is-not-a-silver-bullet/)
- A widely repeated claim that accuracy rose "from 17% to 63%" for a mean-reversion strategy has no primary source. Do not use it. — [mental-momentum aggregator](https://research.mental-momentum.ai/r/meta-labeling-triple-barrier-methods-lq4i7w)
- Overlapping triple-barrier labels leak information across naive train/test splits, so purged and embargoed CV is required. — [LuxAlgo: label definition & horizon](https://www.luxalgo.com/library/concept/label-definition-and-prediction-horizon.md)

### Inferences
- Meta-labeling fits this bot's architecture. The rules already supply the side, and the ML only needs to learn P(win | context), using features such as session, ATR percentile, spread, distance to news events, LLM news score, regime label and time since last trade. The likely gain is fewer bad trades (higher precision, lower drawdown), not more alpha. Improvement depends on the primary signal already having some edge.
- Data requirement: each trade is one label. With about 1,000-3,000 trades split across walk-forward folds, use only low-capacity models: logistic regression, shallow gradient boosting (depth 2-3, strong regularization) or a simple calibrated threshold on 3-8 features. Deep models are not viable at this size.
- Main pitfalls:
  - leakage from overlapping trades and from features computed with future bars;
  - selecting the probability threshold in-sample;
  - concept drift (gold's regime in 2024-2026 differs from earlier years);
  - judging the filter on accuracy instead of net expectancy per trade and total P&L after the reduced trade count;
  - poor probability calibration when the output is used for sizing (calibrate with isotonic or Platt scaling inside each training fold only).
- Benchmark the filter against simple non-ML filters, such as "skip trades within X minutes of high-impact news" or "skip when spread > k × median". Often these capture most of the gain.

### Gaps
- No independent, peer-reviewed, net-of-cost out-of-sample study of meta-labeling on FX or gold intraday strategies was found.
- The quantitative results of Joubert's JFDS papers could not be retrieved.

## 2. Regime classification (HMM, clustering, gradient boosting) for strategy switching

### Takeaway
Grade: MIXED. HMM / Markov-switching regime models have decades of academic support for improving risk-adjusted returns. Most of that is daily or monthly, multi-asset or carry-trade allocation, not intraday FX or gold strategy switching. No FX+gold intraday out-of-sample study was found. Regime labels are most defensible as features or risk throttles (volatility-state sizing), not as hard switches between strategies.

### Cited Findings
- An HMM applied to the FX carry trade, used to decide when to open and close positions, more than doubled the mean-semivolatility ratio relative to the benchmark carry trade in constructed backtests. The abstract does not clearly say the test was out-of-sample. — [Princeton: dynamic allocations for currency futures under switching regimes](https://collaborate.princeton.edu/en/publications/dynamic-allocations-for-currency-futures-under-switching-regimes-/)
- A multi-asset HMM study covering commodities, currencies, stocks and fixed income reports out-of-sample detection of bull, bear and high-volatility periods and improved risk-adjusted returns. — [Brunel (BURA)](https://bura.brunel.ac.uk/handle/2438/7898?mode=full)
- An HMM regime strategy on US, Japanese and German equity indices was reported as profitable out-of-sample after transaction costs. — [MPRA 21154](https://mpra.ub.uni-muenchen.de/21154/1/MPRA_paper_21154.pdf)
- A KAIST thesis found that including market regimes improved out-of-sample portfolio performance. — [KAIST](https://dspace.kaist.ac.kr/handle/10203/221194)
- Gold-market predictability has been studied with an HMM-based Ornstein-Uhlenbeck model. No out-of-sample gold trading results were visible. — [HKU Hub](https://hub.hku.hk/handle/10722/249848)
- A retail blog's HMM rotation among SPY, TLT and GLD reports 19.41% annualized return and Sharpe 1.22 (Nov 2004 to Jan 2026). It is self-reported, with unclear out-of-sample status, so it is anecdotal. — [Medium](https://medium.com/@Splendor001/regime-based-portfolio-allocation-a-hidden-markov-model-approach-to-tactical-asset-rotation-4ff3fdf6f9f8)

### Inferences
- Volatility regimes are persistent and therefore forecastable. Direction regimes are not reliably forecastable. A 2-3 state HMM or a simple realized-volatility percentile on returns and ATR is likely to identify vol states about as well as complex classifiers. The practical uses are scaling size down or pausing mean-reversion engines in high-vol, trending states, and vice versa.
- HMM pitfalls:
  - Smoothed (forward-backward) state probabilities use future data. Only filtered (forward-only) probabilities are valid for backtests.
  - Refitting changes the meaning of state labels (label switching), so states must be re-identified by their mean or volatility after each refit.
  - Regime switching adds degrees of freedom, so every parameter tried counts as a trial for the overfitting statistics in section 6.
- Gradient boosting or clustering "regime" models are effectively meta-labeling with regime features. Fold them into the section 1 filter instead of building a separate switching layer.

### Gaps
- No peer-reviewed intraday XAUUSD or major-FX regime-switching study with net-of-cost out-of-sample results was found.
- No direct comparison of HMM vs. clustering vs. GBM regime detectors in FX or commodities was found.

## 3. LLM-based news and sentiment for gold and FX

### Takeaway
Grade: MIXED for equities, WEAK for gold and FX. The strongest LLM-sentiment evidence (Lopez-Lira & Tang) is on individual stocks. Even there, most of the information is in the non-tradable initial reaction, the tradable drift is concentrated in small caps and negative news, and returns decline as adoption grows. No peer-reviewed study showing LLM news sentiment yields tradable intraday edge in XAUUSD or major FX was found. Careful 2025-2026 reproductions show reported alpha from LLM trading agents largely disappears after costs and contamination controls.

### Cited Findings
- Lopez-Lira & Tang ("Can ChatGPT Forecast Stock Price Movements?", v6 Oct 2025): on post-knowledge-cutoff headlines, GPT-4 captures initial market responses with about 90% portfolio-day hit rates for the non-tradable initial reaction. Predictive power for the subsequent drift is concentrated in small caps and negative news. Forecasting ability rises with model size. Strategy returns decline as LLM adoption rises. Earlier models (GPT-1, GPT-2, BERT) did not forecast returns well. — [arXiv 2304.07619v6](https://arxiv.org/html/2304.07619v6); [v1 abstract](https://arxiv.org/abs/2304.07619v1)
- Glasserman & Lin (2023) identify two biases in LLM sentiment backtests: look-ahead (the model knows what happened after a headline) and "distraction" (general knowledge about the company skews its sentiment reading). In-sample, anonymized headlines performed better, suggesting distraction outweighed look-ahead. Out-of-sample, look-ahead is less of a concern but distraction can persist. They propose entity anonymization as a fix. — [arXiv 2309.17322](https://arxiv.org/abs/2309.17322)
- ChronoBERT / ChronoGPT train chronologically consistent LLMs on point-in-time data only. They find look-ahead bias in news-based return forecasting is modest but model- and application-dependent. — [arXiv 2502.21206v2](https://arxiv.org/html/2502.21206v2)
- StockBench (Oct 2025), a contamination-free multi-month stock trading benchmark, found that most LLM agents trail buy-and-hold, though a few did better on returns and risk control. — [arXiv 2510.02209](https://arxiv.org/abs/2510.02209v1)
- "The Alpha Illusion" (May 2026) re-ran TradingAgents and QuantAgent on 2025-2026 data with realistic costs:
  - TradingAgents' gross profitability shrank after costs, and it underperformed buy-and-hold on 4 of 5 stocks.
  - QuantAgent's Sharpe went negative.
  - The authors attribute inflated alpha to temporal contamination (weights, post-training and retrieval corpora absorbing post-event news and price-path summaries), unmodeled frictions and short evaluation windows.
  - Source: [alphaxiv 2605.16895](https://www.alphaxiv.org/abs/2605.16895) (abstract-level only; full text not fetchable)
- AlphaForgeBench argues that single-run live LLM evaluations are statistically unreliable, because of LLM stochasticity and a single execution window. — [arXiv 2602.18481](https://arxiv.org/pdf/2602.18481)
- "The New Quant" survey (Oct 2025) flags temporal leakage and hallucination as production challenges. It calls for evaluations that report costs, latency and capacity. — [arXiv 2510.05533](https://arxiv.org/html/2510.05533v1)
- BloombergGPT beat similar-sized models on financial NLP tasks such as sentiment and headline classification, except NER. Its 43% ConvFinQA exact-match accuracy trailed GPT-4 zero-shot (69-76%). This suggests that general frontier LLMs match or beat domain-tuned financial LLMs on reading tasks. — [LLMs in Finance survey, arXiv 2311.10723](https://arxiv.org/html/2311.10723v2)
- Gold-specific LLM evidence is informal. BullionVault's annual comparison of AI chatbot gold-price forecasts (Gemini, ChatGPT, later Perplexity, Meta AI, Claude) against users and LBMA analysts is a small, non-controlled exercise. Vendor "LLM-driven gold sentiment" posts (e.g., Permutable AI) are marketing. — [BullionVault](https://www.bullionvault.com/about-us/in-the-press/2025/de-fak-ai-aug); [Permutable AI](https://permutable.ai/resource-hub/page/23/)
- FX reacts at high frequency to FOMC events. Lee & Wang (2025, RAPS) find post-FOMC reversals offset about 65% of pre-announcement USD drift, mostly 12-24 hours later, and FX responds more strongly to expansionary announcements. Price action around scheduled macro events is the dominant "news" driver for FX and gold, and it is already priced in seconds. — [Lee & Wang RAPS 2025 via IDEAS](https://ideas.repec.org/a/oup/rasset/v15y2025i3-4p247-287..html); [Borsa Istanbul Review](https://doaj.org/article/91ee4c3f98784d39bae5a656fe5bc5c7)

### Inferences
- For a retail bot, LLM latency (seconds to tens of seconds via API) plus retail news-feed latency means the initial reaction to scheduled releases (NFP, CPI, FOMC) is not capturable. Lopez-Lira & Tang's own result says the initial reaction is "non-tradable". The realistic value of the LLM news reader is:
  - risk gating: blocking or flattening around high-impact or surprise events, and detecting unscheduled geopolitical shocks;
  - a categorical context feature (e.g., "hawkish Fed surprise", "risk-off geopolitical") fed into the meta-label filter;
  - not a standalone directional signal.
- Look-ahead pitfalls in backtesting the news reader:
  - the LLM's training data covers the backtest period;
  - news timestamps are publication time, not the time the bot would have received the item;
  - headlines are revised or re-published;
  - vendor "sentiment" archives are recomputed later with newer models.

  Mitigations: test only on news after the model's knowledge cutoff, record live LLM outputs with timestamps going forward (the cleanest dataset), anonymize or strip dates where feasible, and pin the model version.
- LLM output is stochastic. Use temperature 0, a fixed prompt version and logged outputs, and treat any prompt or model change as a new strategy version that needs re-validation.

### Gaps
- No peer-reviewed 2023-2026 study measuring LLM news-sentiment trading performance specifically on XAUUSD or major FX pairs intraday, net of costs, was found.
- No FinGPT trading-performance numbers were retrieved.
- No LLM-on-FOMC-text intraday FX study was found by this search.

## 4. Deep learning and time-series foundation models for short-horizon forecasting

### Takeaway
Grade: WEAK for beating baselines after costs at intraday horizons. The random walk remains hard to beat in FX. Published deep-learning FX edges are small and often vanish at a few bps of cost. 2025-2026 benchmarks of foundation models (TimesFM, Chronos, Chronos-2, Moirai, TTM) conclude they are useful low-data priors, especially for volatility, but not reliable alpha engines for returns. Volatility forecasting is the defensible use.

### Cited Findings
- A 2026 benchmark of pretrained TSFMs (TimesFM-2.5, Chronos, Chronos-2 and others, zero-shot) against train-from-scratch PatchTST and N-BEATS, on 20-business-day US equity returns, concludes TSFMs are "useful practical priors that reduce model-development costs in low-data financial forecasting, but are not universal engines for statistically reliable alpha generation". — [arXiv 2606.27100](https://arxiv.org/pdf/2606.27100); [IDEAS](https://ideas.repec.org/p/arx/papers/2606.27100.html)
- Marconi (Jul 2025) tested Tiny Time Mixers and Chronos on Treasury yield changes, EUR/USD volatility and equity spreads. Zero-shot TTM outperformed naive benchmarks in volatility forecasting and spread prediction. — [arXiv 2507.07296](https://arxiv.org/abs/2507.07296v1)
- Goel et al. (May 2025) evaluated TimesFM for realized-volatility forecasting, zero-shot and fine-tuned, against econometric benchmarks. Results were not visible in the snippet. — [EconPapers 2505.11163](https://econpapers.repec.org/paper/arxpapers/2505.11163.htm)
- A 2026 paper on multivariate Chronos-2 for financial data hypothesizes that multivariate inputs help only when properly chosen and aligned. — [arXiv 2605.21504](https://arxiv.org/pdf/2605.21504)
- Momentum Transformer paper: the FX sleeve's LSTM Sharpe falls from 0.11 at zero cost to -0.46 at 3 bps per trade, and the authors note deep momentum networks often degrade net of costs. — [arXiv 2112.08534](https://arxiv.org/pdf/2112.08534)
- EXFormer (Dec 2025) claims to beat the random walk on daily EUR/USD, USD/JPY and GBP/USD. Gains shrink after conservative costs and slippage. These are daily, not intraday, results. — [arXiv 2512.12727](https://arxiv.org/html/2512.12727v1)
- Hourly BTC-USDT study (2026): XGBoost, LSTM and iTransformer showed positive gross returns, but naive sign-based strategies failed at 10 bps costs. A cost-aware filter that trades only when the forecast exceeds the cost threshold restored profitability in some configurations. — [chatpaper summary](https://chatpaper.com/chatpaper/paper/289469)
- Dempster et al. (2001), intraday FX with technical indicators: all methods were significantly profitable in- and out-of-sample at zero cost, none at realistic costs, and there is "substantial danger of overfitting" if in-sample learning is unconstrained. — [Dempster 2001](https://gpbib.pmacs.upenn.edu/gp-html/Dempster_2001_trading.html)

### Inferences
- With a few thousand trades and retail spreads (XAUUSD spreads of roughly tens of cents on a $2,000-4,000+ price), any intraday return forecaster must clear costs that are large relative to typical forecast edges. Training LSTMs or Transformers on retail-scale data is likely to overfit.
- Low-cost, realistic uses:
  - zero-shot TSFM or GARCH/HAR volatility forecasts to set stops, targets and position size;
  - a forecast-exceeds-cost gate (the BTC study's idea) as one feature in the meta-label filter.

  Always benchmark against a random walk / no-trade baseline and against simple EWMA or HAR volatility.

### Gaps
- No direct intraday XAUUSD or major-FX study of TSFMs (TimesFM / Chronos / Moirai) net of costs was found.
- Full quantitative results of 2606.27100 and 2507.07296 could not be fetched.

## 5. Reinforcement learning for trading

### Takeaway
Grade: WEAK. RL trading literature is dominated by unrealistic assumptions (no costs, no spread, no liquidity limits), inconsistent evaluation and overfitting. Where costs are included, profitability relative to baselines erodes sharply. RL is the least suitable method for a small retail bot with limited history.

### Cited Findings
- Meng & Khushi (2019) systematic review of RL in stock and FX trading: all reviewed articles had unrealistic assumptions (no transaction costs, liquidity or bid/ask issues), costs significantly reduced RL profitability versus baselines, and the field is "still in its early development". — [Data journal via IDEAS](https://ideas.repec.org/a/gam/jdataj/v4y2019i3p110-d252469.html)
- Millea (2021) critical DRL survey (FX, crypto, equities): lack of consistency in the community impedes reliable development of DRL trading agents. — [MDPI Data 6(11):119](https://mdpi-res.com/d_attachment/data/data-06-00119/article_deploy/data-06-00119-v2.pdf)
- RL trading survey (arXiv 2212.06064): transaction costs are essential for realism, and some studies omit or fail to specify cost assumptions. — [arXiv 2212.06064](https://arxiv.org/pdf/2212.06064)
- A 2025 FX RL preprint names noisy data, overfitting risk and poor generalization to new market conditions as key challenges. — [preprints.org](https://www.preprints.org/manuscript/202111.0044)
- Dempster et al. (2001), already cited in section 4, found that methods including RL-type learners were not significantly profitable on intraday FX at realistic costs. — [Dempster 2001](https://gpbib.pmacs.upenn.edu/gp-html/Dempster_2001_trading.html)

### Inferences
- RL needs a realistic simulator (spread, slippage, swap, latency) and far more independent episodes than a few thousand trades provide. A reward based on backtest P&L is a direct optimizer of overfitting.
- Classify RL as hype for this bot. A contextual-bandit framing (choose which engine or size to use given context, learned conservatively) is the only RL-adjacent idea worth considering, and it collapses to meta-labeling plus sizing.

### Gaps
- No 2023-2026 study showing deep RL beating simple baselines on intraday FX or gold net of realistic costs out-of-sample was found.

## 6. Overfitting controls and live / shadow validation

### Takeaway
Grade: STRONG for the methodology. The statistical case for DSR, PBO/CSCV and purged CV is peer-reviewed and well established, and multiple-testing inflation is the main reason retail backtests fail live. Every ML add-on above must pass:

- walk-forward or purged CV;
- a deflated Sharpe that accounts for all variants tried;
- PBO from CSCV where many configurations were tested;
- a pre-registered shadow/paper period with enough trades to reach a decision.

### Cited Findings
- Deflated Sharpe Ratio (Bailey & López de Prado, JPM 2014) corrects for selection bias under multiple testing and for non-normal returns. It is a Probabilistic Sharpe Ratio whose threshold reflects the number of trials. — [SSRN 2460551](https://papers.ssrn.com/abstract=2460551); [eScholarship PDF](https://escholarship.org/content/qt2329p290/qt2329p290.pdf)
- Probability of Backtest Overfitting (Bailey, Borwein, López de Prado, Zhu; J. Computational Finance 2017):
  - Estimated through combinatorially symmetric cross-validation (CSCV): split the performance matrix of all trials into blocks, enumerate train/test combinations, and compute the fraction where the in-sample best lands below the out-of-sample median.
  - No distributional assumptions, but requires the full trial-by-trial record.
  - Sources: [SSRN 2326253](https://papers.ssrn.com/abstract=2326253); [eScholarship PDF](https://escholarship.org/content/qt4w1110bb/qt4w1110bb.pdf); [LuxAlgo explainer](https://www.luxalgo.com/library/concept/probability-of-backtest-overfitting.md)
- CXO Advisory summarizes an example from the Bailey et al. work in which in-sample Sharpe 1.59 became -0.18 out-of-sample. This was not verified against the primary paper. — [CXO Advisory](https://www.cxoadvisory.com/big-ideas/backtest-overfitting-the-movies/)
- Overlapping labels require purging and embargo in CV. — [LuxAlgo](https://www.luxalgo.com/library/concept/label-definition-and-prediction-horizon.md)
- LLM-specific: single-run live evaluations are statistically unreliable, so LLM strategies need repeated runs or long live windows. — [AlphaForgeBench](https://arxiv.org/pdf/2602.18481)

### Inferences
- A practical protocol for this bot:
  1. Log every variant ever tested, including thresholds, features and prompts, as the trial count N for DSR.
  2. Use anchored or rolling walk-forward with retrain cadence fixed in advance. For a meta-label model, use purged k-fold or CPCV with an embargo of at least the maximum trade holding time.
  3. Require DSR > 0.95 and PBO < about 0.2-0.3 (a heuristic) before going live.
  4. Run a shadow A/B: the rule engine without the filter versus the rule engine with the filter, on the same live signals in the same period. Compare per-trade net R or pips after real spread, using a paired test on the overlapping signal set, or a block bootstrap on daily P&L.
  5. Pre-register the stopping rule (e.g., minimum 100-300 live trades, or N weeks) and the metric before starting.
- Minimum trade counts, rough arithmetic rather than a cited source: the standard error of a per-trade Sharpe is about 1/√n. Detecting a per-trade Sharpe improvement of 0.1 at about 2σ needs roughly 400+ trades per arm. Several hundred trades are the floor for meaningful A/B conclusions, and filters that cut trade count make this worse.
- Shadow-test pitfalls:
  - paper fills ignore slippage and requotes on XAUUSD news spikes;
  - non-overlapping periods confound regimes;
  - changing prompts or models mid-test resets the test;
  - survivorship from discarding "bad" weeks.

### Gaps
- The primary text of López de Prado's Minimum Track Record Length / Minimum Backtest Length formulas was not retrieved in this session; the numbers above are inference.
- No retail-specific study of paper-vs-live slippage for XAUUSD was found.

## 7. Position sizing and risk ML (volatility targeting, Kelly fractions)

### Takeaway
Grade: STRONG that volatility targeting reduces tail risk in all asset classes, and STRONG that it does not raise Sharpe for currencies and commodities. Grade: MIXED-to-STRONG (theory plus practitioner consensus) that fractional Kelly beats full Kelly under estimation error. For this bot, vol-scaled sizing with a capped fractional-Kelly overlay is the highest-confidence, lowest-complexity improvement. ML adds little beyond a good volatility forecast.

### Cited Findings
- Harvey, Hoyle, Korgaonkar, Rattray, Sargaison & Van Hemert, "The Impact of Volatility Targeting" (JPM 2018, 60+ assets, data back to 1926):
  - Sharpe-ratio benefits are confined to risk assets (equities, credit).
  - For bonds, currencies and commodities the Sharpe effect is negligible.
  - Volatility targeting reduces the likelihood of extreme returns across all asset classes.
  - Sources: [Duke PDF](https://people.duke.edu/~charvey/Research/Published_Papers/P135_The_impact_of.pdf); [Quantpedia summary](https://quantpedia.com/the-impact-of-volatility-targeting-on-equities-bonds-commodities-and-currencies); [CXO Advisory](https://www.cxoadvisory.com/equity-premium/benefits-of-volatility-targeting-across-asset-classes)
- Fractional Kelly:
  - Edges estimated from limited samples are usually overestimated, and full Kelly assumes exact knowledge of win probability and payoff.
  - Overbetting is asymmetrically costly: by a common calculation, 2× Kelly yields about zero long-run growth, while 0.5× keeps about 75% of optimal growth.
  - Half Kelly is often cited as about 75% of growth for much lower variance.
  - These are secondary or practitioner sources and drawdown tables are unsourced.
  - Sources: [LuxAlgo: Kelly](https://www.luxalgo.com/library/concept/kelly-criterion/); [claudeskills Kelly reference](https://claudeskills.info/skills/agiprolabs/claude-trading-skills/kelly-criterion/); [FlashAlpha](https://flashalpha.com/tools/kelly-criterion-calculator); [NexusFi](https://nexusfi.com/a/risk-management/kelly-criterion)
- Calibrated meta-label probabilities can feed bet sizing. This needs well-calibrated probabilities. — [Joubert JFDS listing](https://ae.linkedin.com/in/jacquesjoubert); [Jesse docs](https://docs.jesse.trade/docs/research/ml/meta-labeling)

### Inferences
- For XAUUSD and FX, expect vol targeting to smooth the equity curve and cut tail losses, not to raise Sharpe.
- Recommended stack:
  - risk per trade = f(forecast volatility), using an ATR/EWMA/HAR or zero-shot TSFM volatility forecast;
  - a fractional Kelly cap of about 0.25-0.5× Kelly from conservative (shrunk) edge estimates, recomputed walk-forward;
  - optionally, modest scaling by calibrated meta-label probability, clipped to a narrow band (e.g., 0.5×-1.5× base risk);
  - hard daily loss and exposure limits independent of ML.
- With a few thousand trades, Kelly inputs (win rate, payoff ratio) have wide confidence intervals. Use the lower confidence bound of the edge, not the point estimate.

### Gaps
- Peer-reviewed fractional-Kelly-under-parameter-uncertainty work (e.g., MacLean, Thorp & Ziemba) was not retrieved in this session.
- No study of ML-driven sizing specifically for intraday gold was found.

## Summary grading table

| Method | Evidence grade | Retail feasibility (few thousand trades) | Verdict |
|---|---|---|---|
| Vol-targeted sizing + fractional Kelly cap | Strong (tail-risk reduction; no Sharpe gain in FX/commodities) | High | Do it |
| Overfitting controls (walk-forward, purged CV/CPCV, DSR, PBO, pre-registered shadow A/B) | Strong (methodology) | High | Mandatory |
| Meta-labeling / ML trade filter on rule signals | Weak-to-mixed | Medium-high (low-capacity models only) | Worth a disciplined trial |
| Regime detection (vol-state HMM / percentile) as a feature or throttle | Mixed | High | Use as a feature or risk throttle, not a hard switch |
| LLM news as risk gate / context feature | Mixed (equities), weak (FX/gold) | High | Keep as gate or feature; not an alpha source |
| LLM news as standalone directional signal | Weak (alpha largely gone after costs and contamination controls) | Low | Hype at intraday retail latency |
| TSFMs for volatility forecasting | Mixed (promising) | High (zero-shot) | Try against HAR/EWMA |
| LSTM/Transformer/TSFM for intraday return direction | Weak after costs | Low | Hype for this use |
| Deep RL trading agent | Weak | Very low | Hype; avoid |
