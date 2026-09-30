# Arconian: Systematic Signal Exploitation in Informationally Delayed Equities
### Architecture, Statistical Foundation & Implementation Specification — v3

---

## Abstract

Arconian is a retail-scale systematic trading system targeting short-term (1–3 day) mispricings in U.S. small-cap equities exhibiting measurable information processing delays. The system's edge thesis rests on the empirically documented slow-diffusion anomaly (Hong & Stein 1999; Hou & Moskowitz 2005): stocks with thin analyst coverage and low institutional participation incorporate market-wide and firm-specific signals more slowly, creating exploitable windows where price has not yet reflected available information. Unlike speed-based strategies, this approach competes on **signal identification in structurally neglected names** — a domain where institutional capacity constraints and HFT disinterest create a persistent niche.

This document supersedes v2 and incorporates a comprehensive adversarial review addressing twenty-five identified failure modes spanning edge thesis validity, universe construction, signal methodology, risk calibration, execution architecture, cost modeling, and machine learning pipeline design. Every material change from v2 is documented with rationale.

The system is governed by seven operating principles:

1. **Transaction costs are the primary adversary, not other traders.** Every design decision is filtered through a cost-aware lens. If a signal cannot survive 120–160 bps of round-trip friction (revised upward from v2's 100 bps estimate), it is discarded regardless of statistical significance.
2. **Signals must be continuous, not binary.** Thresholding destroys information. The system scores candidates on a continuous composite scale, weighted by each signal's empirical information coefficient.
3. **Risk management must account for fat tails and correlation spikes.** Normal-distribution assumptions are rejected throughout. Position sizing, risk measurement, and circuit breakers are calibrated to the actual return distributions of small-cap equities.
4. **Machine learning is a Phase 2 enhancement, not a Phase 1 dependency.** The rule-based system must demonstrate positive expectancy on its own before any ML overlay is introduced. Data collection for ML training begins immediately but model deployment is deferred for 12+ months.
5. **The system must be auditable end-to-end.** Every signal, every sizing decision, every outcome is logged with full provenance. The journal is not a nice-to-have — it is the mechanism by which the system learns whether it has an edge.
6. **Academic effect sizes must be discounted for post-publication decay.** All expectancy projections apply a 50% haircut to literature-derived effect sizes, reflecting McLean & Pontiff (2016) evidence that anomalies decay ~58% on average after publication.
7. **After-tax returns are the only returns that matter.** All performance metrics are reported both pre-tax and after-tax. System viability is assessed on after-tax expectancy, and operational structure is optimized for tax efficiency.

---

## 1. Edge Thesis: Information Decay Lag in Structurally Neglected Equities

### 1.1 Theoretical Foundation

The efficient market hypothesis assumes information is incorporated into prices instantaneously and uniformly. In practice, the speed of price adjustment varies dramatically across the equity universe as a function of analyst coverage, institutional ownership, and trading activity.

Hong & Stein (1999) formalized this as the **gradual information diffusion model**, predicting that stocks where firm-specific information spreads slowly will exhibit stronger short-term momentum and more pronounced post-event drift. Hong, Lim & Stein (2000) confirmed this empirically: momentum profits are significantly larger among low-analyst-coverage stocks, with the differential reaching 0.33%/month at ~$200M market cap. Coverage is an independent explanatory variable after controlling for size.

Hou & Moskowitz (2005) made the phenomenon directly measurable by introducing the **price delay metric**: the fraction of a stock's market-related return variation that is explained by lagged rather than contemporaneous market returns. They demonstrated that high-delay firms earn a return premium unexplained by size, liquidity, or book-to-market, and that post-earnings announcement drift increases monotonically with delay. This is the core mechanism: in high-delay stocks, catalysts (earnings surprises, unusual volume, sector rotation) take longer to be fully reflected in price, creating a window for systematic capture.

Chen & Lu (2016) extended this to the options market, showing that options-implied signals predict equity returns specifically among high-delay stocks, generating risk-adjusted alpha of 1.8%/month. However, this finding relies on proprietary direction-tagged data from CBOE's signed volume dataset. Pan & Poteshman (2006) demonstrated that publicly observable options signals carry substantially less predictive power than proprietary data — a constraint that directly governs the weight assigned to the options signal in Arconian's scoring architecture (see Section 3.2.3).

### 1.2 Post-Publication Decay Adjustment

McLean & Pontiff (2016) documented that anomaly returns decline by approximately 58% post-publication, driven by both statistical artifacts (in-sample overfitting) and genuine learning by market participants. Asness, Frazzini & Moskowitz (2014) specifically cautioned that the small-cap momentum differential may be more period-dependent than the original Hong et al. results suggested.

**Arconian applies a blanket 50% haircut to all literature-derived effect sizes.** The original 0.33%/month differential becomes a working estimate of ~0.16%/month. The original gross edge estimate of 200–400 bps per trade is revised to **100–200 bps per trade** before costs.

This is a deliberately conservative adjustment. The actual decay may be less severe in the specific universe Arconian targets (structurally neglected small caps with thin coverage) because the participants best positioned to arbitrage the anomaly — institutional investors — face the same capacity constraints that create the anomaly. Nevertheless, designing to a halved edge ensures the system is viable even under pessimistic assumptions about edge persistence.

### 1.3 Post-2020 Microstructure Regime

The v2 whitepaper did not address how post-COVID market microstructure changes affect the information diffusion speed in small caps. Three developments are material:

**Retail scanner proliferation.** Tools like Trade Ideas, Unusual Whales, Finviz, and social platforms (StockTwits, Reddit r/wallstreetbets) surface unusual volume and options activity to millions of users simultaneously. The exact names Arconian targets — $300M–$2B with unusual volume/options flow — are the names these tools flag. When Unusual Whales alerts on a name, thousands of retail traders pile in within minutes, compressing the information delay window.

**Zero-commission trading and options democratization.** The elimination of commissions and rise of fractional shares has dramatically lowered the barrier to acting on publicly visible signals. A 2019-era information delay of 2–3 days may now be 0.5–1.5 days for names that attract retail attention.

**0DTE options explosion.** The growth in ultra-short-dated options has altered the informational content of options flow data. A significant fraction of small-cap options volume is now speculative 0DTE activity rather than informed positioning, adding noise to the options flow signal.

**Mitigation:** Arconian incorporates a **retail attention filter** in the signal layer (Section 3.2.6) that penalizes names already surfaced by major retail scanners and social platforms. Additionally, the expected holding period is calibrated to 1–3 days with awareness that the effective window may be shorter than historical norms suggest, reinforcing the need for rapid signal processing and execution.

### 1.4 Why the Edge Persists (With Caveats)

Three structural factors protect this niche from rapid arbitrage:

**Capacity constraints.** Institutional funds managing $500M+ cannot meaningfully allocate to positions in $300M–$2B companies without market impact. A $1B fund taking a 1% position in a $400M company would represent ~2.5% of the float — visible, market-moving, and illiquid to exit. This is precisely why analyst coverage remains thin.

**HFT disinterest.** High-frequency strategies require deep, continuous liquidity. Small-cap names with $5–15M daily dollar volume and wide spreads are structurally unattractive for latency-sensitive strategies. Co-location advantages are irrelevant when the holding period is 1–3 days.

**Signal complexity.** The edge is not a single exploitable pattern but an ensemble of conditions (unusual activity + directional confirmation + regime context + delay structure + corporate action filtering + retail attention adjustment) that requires multi-factor analysis. Simple momentum or mean-reversion strategies in isolation are increasingly crowded; the combination with delay-based filtering, earnings awareness, and corporate action exclusions is substantially less so.

**Honest caveat on persistence:** Strategy lifespan research estimates 18–24 months before a given parametric approach decays as other participants adopt similar methods. Arconian must therefore be designed for continuous recalibration: parameters are not fixed constants but are re-estimated on rolling windows, and the signal weighting scheme adapts to shifting information coefficients. The 50% haircut on academic effect sizes (Section 1.2) partially pre-accounts for this decay, but the system must monitor for further deterioration in real time.

### 1.5 Revised Edge Magnitude Estimates

| Metric | v2 Estimate | v3 Estimate (Post-Haircut) | Basis |
|---|---|---|---|
| Gross edge per trade | 200–400 bps | 100–200 bps | 50% haircut per McLean & Pontiff (2016) |
| Round-trip transaction costs | 60–110 bps | 80–160 bps | Revised adverse selection (Section 5) |
| Net edge per trade | 50–200 bps | 20–80 bps | Conservative: assumes both lower gross edge and higher costs |
| Minimum viable gross edge | 100 bps | 150 bps | Must clear costs + buffer for estimation error |
| Expected win rate | 45–55% | 45–52% | Narrowed upper bound to reflect compressed edge |
| Expected avg winner | 1.5–2.0R | 1.3–1.8R | Slightly reduced to reflect tighter barriers |

These are deliberately conservative. If live trading reveals the edge is larger than projected, the system scales up through the tiered risk mechanism (Section 4.4). If the edge is at the lower bound, the system detects this within 100 trades and triggers a fundamental review before significant capital is lost.

---

## 2. Universe Construction

### 2.1 Investable Universe Criteria

The universe is defined by the intersection of six filters (expanded from v2's four), each addressing a specific failure mode:

| Filter | Criterion | Rationale |
|---|---|---|
| **Market cap** | $500M–$2B | Floor raised from $300M. The $300M–$500M band suffers from midday liquidity mirages (headline ADV overstates executable liquidity) and spreads that consume the compressed edge. Above $2B, information delay diminishes and institutional competition increases. |
| **Daily dollar volume** | ≥ $5M trailing 20-day average | Raised from $3M. At $5M/day, a $7K position is ~0.14% of daily volume — well below the 1% market impact threshold. |
| **Midday liquidity** | ≥ $1.5M dollar volume between 10:30 AM–2:30 PM ET, trailing 20-day average | **New filter.** Addresses the liquidity mirage problem: names with high ADV concentrated in opening/closing auctions but thin midday books produce systematic slippage that exceeds cost model estimates. Execution for 1–3 day swings occurs midday; this is the liquidity that matters. |
| **Bid-ask spread** | ≤ 40 bps average over trailing 20 days | Tightened from 50 bps. With the compressed edge estimate (Section 1.5), every basis point of spread costs matters more. A 40 bps spread contributes ~40 bps per round trip. |
| **Options availability** | Listed options with ≥ 1,500 aggregate OI across near-term expirations, distributed across ≥ 4 strikes | Raised from 500 OI and added strike distribution requirement. At 500 OI concentrated in 2–3 strikes, a single block trade dominates the signal. The 4-strike distribution requirement ensures the options market for the name has sufficient breadth to produce meaningful flow signals. |
| **Trading history** | ≥ 60 trading days of price/volume history | **New filter.** Required for stable percentile rank computation. Names with insufficient history enter "observation mode" (data logged, no scoring). See Section 3.5. |

**Estimated universe size:** 150–300 names, refreshed monthly. The tighter filters reduce the universe from v2's 200–400 estimate but improve the average signal quality and reduce false positive rates. Stocks that lose liquidity, options coverage, or history requirements are dropped; new names meeting all six criteria are added via automated screener.

### 2.2 Short Availability Filter (Bearish Trades Only)

For bearish signal candidates, an additional filter is applied:

**Borrow availability check.** The Schwab API's short selling availability endpoint is queried for each bearish candidate. If the stock is flagged as hard-to-borrow (HTB) or if locate fees exceed 100 bps annualized, the bearish signal is suppressed. If the API does not expose borrow availability, the following heuristic is applied:

- Short interest > 20% of float → flagged as potentially HTB
- Daily short volume > 30% of total volume → flagged as potentially HTB
- Flagged names receive a 50 bps borrow cost adder in all expectancy calculations

For all backtesting of bearish signals, a conservative 50 bps borrow cost per trade is applied as a default, with the actual cost logged during live trading to calibrate the model. If borrow availability data remains unreliable after Phase 1, bearish signals may be restricted to a confirmed-available whitelist.

### 2.3 Price Delay as a Structural Filter

Each stock in the universe receives a **Hou-Moskowitz price delay score** computed as follows:

```
For each stock i, estimate two models using 52 weeks of weekly returns:

  Restricted:   R_i,t = α + β_0 * R_m,t + ε_t
  Unrestricted: R_i,t = α + β_0 * R_m,t + Σ(k=1..4) β_k * R_m,t-k + ε_t

  Delay_i = 1 − (R²_restricted / R²_unrestricted)
```

Stocks with `Delay > 0.3` are flagged as **primary targets** — the slow-diffusion names where the academic evidence is strongest. Stocks with `Delay ≤ 0.3` remain in the scannable universe but receive a scoring penalty (see Section 3.3), reflecting the weaker expected edge. This is not a hard filter but a continuous weighting factor: the system can trade low-delay names if the composite signal is sufficiently strong, but it structurally favors high-delay names where the thesis applies most directly.

The delay score is recomputed monthly and versioned (see Section 7.3). It is expected to be relatively stable (the structural causes — thin coverage, low institutional ownership — change slowly), but regime shifts (e.g., a stock gaining analyst coverage after a strong quarter) will naturally move names out of the primary target set.

### 2.4 Survivorship-Bias-Free Universe for Historical Analysis

Any historical backtest or signal validation must use a **point-in-time universe** that includes delisted names. Using today's universe to scan historical data introduces survivorship bias — names that met the criteria historically but were subsequently acquired, went bankrupt, or delisted are excluded, and these names often had the most extreme signals before they disappeared.

**Required data source:** Norgate Data (preferred for small-cap coverage with delisting-adjusted returns) or Sharadar (Nasdaq Data Link) with delisted-name coverage. Standard yfinance data does not include delisted names and is not acceptable for any backtest or historical signal analysis. This is a non-negotiable infrastructure requirement, not an optional enhancement.

Budget allocation: ~$500/year for Norgate Data or ~$300/year for Sharadar. This cost is trivial relative to the trading capital at risk and the information value of an unbiased backtest.

---

## 3. Signal Detection & Scoring

### 3.1 Design Philosophy: Continuous Scoring, Not Binary Thresholds

The v1 design used binary z-score thresholds (volume z > 2.0, return z > 1.5, etc.) and required ≥ 2 to fire. This approach has three documented problems:

1. **Z-scores assume normality.** Small-cap daily returns follow approximately a Student-t distribution with ~3 degrees of freedom (Welch 2024). At z = 2.0 under the actual distribution, tail events occur 5–10x more frequently than the Gaussian assumption implies, flooding the candidate pool with false signals.
2. **Binary thresholds destroy information.** A stock with volume at the 96th percentile and another at the 99.9th percentile would receive identical scores under thresholding. The difference between those two events is enormous.
3. **Equal weighting ignores signal quality.** Volume has substantially more academic support as a return predictor than ATR expansion, which has essentially none as a directional signal. Weighting them equally dilutes the stronger signal.

The revised system replaces this with **percentile-rank-based continuous scoring**, which is distribution-free (no normality assumption required) and preserves the full information content of each signal. **All signals use percentile ranks — no z-scores anywhere in the scoring pipeline.** This addresses the internal inconsistency identified in v2 where Sector RS used a z-score while all other signals used percentile ranks.

### 3.2 Signal Components

Each signal is computed as a **percentile rank** against the stock's own trailing history, producing a value in [0, 1] that is directly comparable across signals and across stocks.

#### 3.2.1 Volume Surprise (Initial Weight: 0.30)

```
volume_percentile = mean(
    percentile_rank(today_volume, trailing_20d_volume),
    percentile_rank(today_volume, trailing_60d_volume),
    percentile_rank(today_volume, trailing_120d_volume)
)
```

Academic basis: Gervais, Kaniel & Mingelgrin (2001) documented a high-volume return premium, stronger when high volume is not accompanied by extreme returns. Kaniel, Li & Starks (2012) confirmed this across 41 countries. The multi-window approach (20/60/120 day) follows ReSolve Asset Management's finding that short lookbacks underperform due to high estimation error, while longer lookbacks capture structural regime shifts in trading activity.

If the 20-day rank is high but the 120-day rank is moderate, the event is a short-term anomaly; if all three are high, it represents a structural regime change. For stocks with fewer than 120 days of history (but ≥ 60 days per the universe filter), the 120-day window is replaced with whatever history is available, and the signal is flagged as "short-history" in the signal log for separate IC analysis.

#### 3.2.2 Return Magnitude (Initial Weight: 0.25)

```
return_percentile = percentile_rank(abs(1d_return), trailing_60d_abs_returns)
```

The absolute value captures the magnitude of the move regardless of direction; directional interpretation is handled separately in the confirmation layer (Section 3.4). Using absolute returns avoids the asymmetry problem where large negative returns and large positive returns have different distributional properties.

#### 3.2.3 Options Flow Composite (Initial Weight: 0.15)

**Weight reduced from v2's 0.25 to 0.15.** This reflects the Pan & Poteshman (2006) finding that publicly observable options signals carry substantially less predictive power than the proprietary direction-tagged data used in Chen & Lu (2016). The weight may be adjusted upward if IC recalibration (Section 3.3) demonstrates stronger predictive power than expected in the live trading universe, but the initial allocation reflects a realistic assessment of publicly available options data quality. **Accelerated recalibration for options:** Unlike other signals whose weights are reviewed every 60 trading days, the options weight is reviewed every 30 trading days during the first 6 months of live operation. If the options IC exceeds the volume-weighted average IC of the other signals for two consecutive 30-day periods, the options weight is increased by 0.05 (to a maximum of 0.25). This faster feedback loop ensures the options signal can earn its way back to a higher weight quickly if it proves more informative than the Pan & Poteshman baseline suggests in Arconian's specific universe — without requiring a structural redesign.

This signal is computed only for stocks meeting the enhanced options liquidity threshold (≥ 1,500 aggregate OI across ≥ 4 strikes). For stocks without sufficient options data, the signal is omitted and the remaining signals are re-weighted proportionally.

**Additional minimum volume filter:** If total daily options volume for a name is < 200 contracts, the options composite is suppressed entirely for that day regardless of OI. Below 200 contracts, individual transactions dominate the signal and the false positive rate is unacceptable.

```
pc_ratio_percentile = percentile_rank(call_vol / put_vol, trailing_60d_pc_ratios)
iv_rank = (current_IV - 52w_low_IV) / (52w_high_IV - 52w_low_IV)
vol_oi_percentile = percentile_rank(total_vol / total_OI, trailing_60d_vol_oi)

options_composite = 0.4 * pc_ratio_percentile + 0.3 * iv_rank + 0.3 * vol_oi_percentile
```

**IV rank safeguard:** If `(52w_high_IV - 52w_low_IV) < 5 percentage points`, the IV rank is set to 0.5 (neutral) and the event is flagged as `IV_range_insufficient` in the signal log. This prevents numerically unstable IV rank values when IV has been stable for the trailing year (division by near-zero denominator).

**Hedging filter:** If the Volume/OI ratio spikes on a single strike while remaining normal across others, and the stock has recent 13F filings showing large institutional holders, the signal is flagged as potential hedging activity and downweighted by 50%.

**Trade size diversity filter (new):** Options activity for the day must include ≥ 3 distinct trade sizes to pass. If all volume is 1–2 block prints, it is more likely institutional hedging or a single actor than distributed informed flow; the options composite is suppressed.

**Critical interpretation caveat:** Raw put/call ratios at extremes function as **contrarian** indicators. The system does not naively interpret high call volume as bullish; instead, it looks for *unusual* call or put activity relative to the stock's own baseline, then cross-references with the directional confirmation layer. The Pan & Poteshman limitation is why this signal receives the lowest weight among the primary signals and is monitored separately for IC decay.

#### 3.2.4 Relative Sector Strength (Initial Weight: 0.10)

```
sector_spread = stock_5d_return - sector_ETF_5d_return
sector_rs_percentile = percentile_rank(sector_spread, trailing_60d_sector_spreads)
```

**Changed from z-score to percentile rank** to maintain methodological consistency with all other signals. The z-score formulation in v2 was an internal contradiction — the system argued against z-scores for normality violations, then used one for Sector RS. The percentile rank formulation is distribution-free and directly comparable with all other signal components.

This signal isolates firm-specific moves from sector-wide rotations. A stock moving sharply while its sector is flat is more likely exhibiting firm-specific information flow than one moving in lockstep with its sector.

#### 3.2.5 Price Delay Score (Initial Weight: 0.10)

```
delay_signal = delay_score  # from Section 2.3, range [0, 1]
```

This is static (recomputed monthly, versioned) and serves as a structural prior: all else equal, a signal in a high-delay stock is more likely to represent an exploitable information lag than the same signal in a low-delay stock. The delay score used in scoring is always the most recently computed value that predates the signal event (see Section 7.3 for versioning requirements).

#### 3.2.6 Retail Attention Penalty (Initial Weight: 0.10)

**New signal.** This is a negative signal — higher retail attention reduces the composite score, reflecting the thesis that names already widely flagged by retail scanners and social platforms have compressed information delay windows.

```
social_velocity = percentile_rank(
    mentions_24h,           # StockTwits + Reddit mentions in trailing 24 hours
    trailing_60d_mentions   # baseline mention rate for this stock
)

scanner_flag = 1.0 if ticker appeared on major scanner platforms
               (Trade Ideas Top List, Unusual Whales flow alerts,
                Finviz unusual volume) in trailing 24 hours
               0.0 otherwise

short_interest_crowd = 1.0 if SI > 20% of float, 0.0 otherwise

retail_attention = 0.5 * social_velocity + 0.3 * scanner_flag + 0.2 * short_interest_crowd

# Applied as PENALTY: higher retail_attention REDUCES composite score
retail_penalty = retail_attention  # range [0, 1], where 1 = maximum penalty
```

Rationale: The information delay thesis assumes price adjustment is slow because few participants are paying attention. When a name is trending on social platforms, flagged by scanner tools, or has high short interest attracting retail crowd behavior, the delay window compresses. The system should structurally disfavor names where the crowd has already arrived.

Data sources: StockTwits API (free tier), Reddit API (or third-party aggregator), Finviz unusual volume page (scrape or API). The scanner_flag for Trade Ideas and Unusual Whales may require subscriptions or proxy indicators. If a data source is unavailable, that component receives a 0.0 value (no penalty applied for that component) and the remaining components are re-weighted.

### 3.3 Composite Score

```
raw_composite = Σ(weight_i × signal_i) for signals 3.2.1 through 3.2.5
penalized_composite = raw_composite × (1 - penalty_weight × retail_penalty)
```

Where `penalty_weight` = 0.20 initially (a maximum 20% reduction for the highest-attention names). This parameter is subject to IC-based recalibration.

If options data is unavailable, the options weight (0.15) is redistributed proportionally across the remaining signals (Volume, Return, Sector RS, Delay). The composite score ranges from 0 to 1.

**Signal weighting is not fixed.** Every 60 trading days, the system recomputes the **information coefficient (IC)** of each signal — the Spearman rank correlation between the signal value at entry and the subsequent 3-day return. Signals whose IC has decayed below 0.02 (essentially zero predictive power) for two consecutive measurement periods are flagged for review. Weights are adjusted toward signals with higher recent IC, subject to a floor of 0.05 per signal (no signal is ever fully zeroed — it may recover).

**Initial weight rationale:**

| Signal | Weight | Justification |
|---|---|---|
| Volume Surprise | 0.30 | Strongest and most replicated academic support (Gervais et al. 2001, Kaniel et al. 2012) |
| Return Magnitude | 0.25 | Strong empirical basis; magnitude captures information arrival |
| Options Composite | 0.15 | Reduced from 0.25 — publicly observable options data has degraded predictive power (Pan & Poteshman 2006) |
| Sector RS | 0.10 | Firm-specific isolation; supporting but not primary |
| Delay Score | 0.10 | Structural prior; recomputed monthly, slow-moving |
| Retail Attention (penalty) | 0.10 effective | Applied as penalty multiplier, not additive weight |

### 3.4 Directional Confirmation Layer

The composite score measures *magnitude of unusualness*. Direction is determined by a separate confirmation layer:

**Bullish:** (1d_return > 0) AND (price > prior_day_closing_VWAP) AND (if options available: call_vol > put_vol)
**Bearish:** (1d_return < 0) AND (price < prior_day_closing_VWAP) AND (if options available: put_vol > call_vol) AND (borrow available per Section 2.2)
**Ambiguous:** Conflicting signals → no trade. The system does not force a directional interpretation.

**VWAP reference change from v2:** The confirmation layer uses **prior day's closing VWAP** rather than the current intraday VWAP. This eliminates the scan-timing dependency identified in the v2 review: intraday VWAP is a cumulative measure whose value depends on when you check it (a stock can be above VWAP at the midday scan and below it at the close scan). The prior day's closing VWAP is a fixed, known quantity at any scan time, providing a consistent reference level across all three daily scans.

The ambiguous case is critically important. Forcing a direction when signals conflict is a primary source of false positives. In backtesting, we expect 30–50% of high-composite-score events to be directionally ambiguous; these are passed over without penalty.

### 3.5 Corporate Action & Earnings Calendar Integration

**New section.** The signal layer integrates two event calendars to prevent systematic false positives:

#### 3.5.1 Earnings Calendar

Each stock's next and most recent earnings date is tracked via the universe manager. The signal log records `days_to_next_earnings` and `days_since_last_earnings` for every candidate event.

**Exclusion zone:** Signals within **1 trading day before or after** an earnings report are excluded from standard scoring. These events are logged with tag `earnings_proximity` but are not scored or traded. The rationale is twofold: (a) pre-earnings volume spikes are predominantly positioning activity with different information content than the delay-diffusion thesis assumes, and (b) post-earnings drift is a distinct anomaly requiring its own parameter set (different holding period, different signal weights, different risk calibration).

**Earnings-adjacent signals (2–5 days post-earnings):** These are scored normally but tagged `post_earnings_drift`. IC analysis segments these events separately to assess whether the information-lag thesis holds in the post-earnings regime or is dominated by the PEAD effect.

Future enhancement (Phase 2+): Build a dedicated PEAD sub-model that scores earnings surprise magnitude, revision breadth, and analyst reaction speed for high-delay stocks. This is a natural extension of the core thesis but requires its own validation.

#### 3.5.2 Corporate Actions

An SEC filings integration monitors for events that produce false-positive signal patterns:

| Filing / Event | Detection Method | Action |
|---|---|---|
| Secondary offering (S-3 filing) | EDGAR full-text search, daily | Exclude from scoring for 5 trading days post-filing. Volume from dilution events has no informational content for the delay thesis. |
| Reverse split | EDGAR 8-K (Item 8.01) or exchange notification | Exclude for 3 trading days post-effective date. Price/volume data is mechanically disrupted. |
| Merger/acquisition announcement | EDGAR 8-K (Item 1.01) | Exclude for duration of pending deal. Price dynamics are driven by deal spread, not information diffusion. |
| SPAC-related action (de-SPAC, redemption deadline) | EDGAR S-1/F-4 or manual watchlist | Exclude for 10 trading days around the event. SPAC dynamics are sui generis. |
| Ticker change | Exchange notification | Pause scoring for 5 trading days. Historical percentile ranks are potentially disrupted by data discontinuity. |

A **manual blocklist** in the shared overrides table allows the operator to flag additional corporate action situations not captured by automated detection. The blocklist is checked before every scoring run.

### 3.6 Observation Mode for New Universe Entrants

Stocks entering the universe with fewer than 120 trading days of history (but meeting the minimum 60-day requirement) enter **observation mode:**

- All available data is collected and logged to the signal_log
- Volume percentile ranks use only available windows (20-day and 60-day if history permits, no 120-day)
- The signal is flagged with `history_status = 'short'` in the log
- The composite score is computed but **not acted upon** — no trade signals are generated
- Once 120 trading days of history are accumulated, the stock transitions to full scoring eligibility

This addresses the cold-start problem: new universe entrants (post-IPO, recently crossed the $500M threshold) often exhibit the most unusual activity but have insufficient history for stable percentile rank estimation. The observation mode collects data for future IC analysis without risking capital on noisy signals.

---

## 4. Risk Management

### 4.1 Design Philosophy: Survive First, Profit Second

The risk framework is built on three empirical facts about small-cap trading:

1. **Daily returns follow fat-tailed distributions** (approximately Student-t with 3–5 degrees of freedom). Events at 3–4 standard deviations occur 5–10x more often than Gaussian models predict.
2. **Correlations spike during drawdowns.** Cambridge Associates data shows small-cap correlations rising to 0.8–0.9 during broad market selloffs, meaning diversification across 5 small-cap names provides far less protection than naive correlation matrices suggest.
3. **Stop-loss orders in illiquid names gap through intended levels.** A stop at $9.50 on a stock that gaps from $10.00 to $8.75 overnight produces a realized loss 5x larger than intended.

### 4.2 Per-Trade Sizing

```
base_risk_per_trade = 0.01 × account_equity    # 1% base, subject to modifiers below
stop_distance = 1.5 × ATR_20                   # volatility-scaled stop
shares = floor(risk_per_trade / stop_distance)
max_position = 0.10 × account_equity            # 10% of account, hard cap
```

**ATR's role is purely mechanical:** ATR is not a directional signal (v1 analysis found essentially zero academic support for ATR expansion as a return predictor). It is a **volatility normalizer** used exclusively for stop placement and position sizing. A high-ATR stock gets a wider stop and fewer shares; a low-ATR stock gets a tighter stop and more shares. The dollar risk is identical.

**Catalyst gap risk premium (new):** If the stock has an identifiable near-term catalyst (earnings within 5 trading days, pending FDA decision, recent 8-K filing of material type, trading halt in the prior session), the ATR multiplier for stop placement and position sizing is increased from 1.5× to 2.25× (a 1.5× premium on the standard 1.5× ATR). This widens the stop and reduces share count, reflecting the empirically higher gap risk for names selected precisely because something unusual is happening.

**Tiered risk based on composite score (new, deferred to Phase 2):**

| Composite Score Percentile | Risk Per Trade | Prerequisite |
|---|---|---|
| 70th–85th | 0.75% | Available from Phase 1 |
| 85th–95th | 1.00% (base) | Available from Phase 1 |
| 95th+ | 1.25% | Deferred until 200+ trades confirm that higher composite scores predict higher win rates |

The tiered risk system is disabled during Phase 1 (all trades use 1.0% base risk) to collect unbiased data on the composite-score-to-outcome relationship. It is activated only after empirical validation that the relationship is monotonic and statistically significant (p < 0.05 on the rank correlation between composite score decile and win rate).

**Regime adjustment:**

| VIX Level | IWM 10-Day Return | Action |
|---|---|---|
| ≤ 25 AND IWM ≥ -5% | Normal | Base risk (1.0%) |
| > 25 OR IWM < -5% | Elevated | Risk reduced to 0.5% per trade |
| > 35 OR IWM 20d < -10% | Crisis | Halt all new position entry |

The IWM-based regime detector is **new in v3** and addresses the failure mode where small-cap-specific selloffs (2022 Q1, 2018 Q4) diverge from broad market VIX levels. The Russell 2000 can underperform the S&P 500 by 5–10% during risk-off rotations while VIX remains below 25. Arconian's target universe would be in drawdown, but the v2 VIX-only circuit breaker wouldn't detect it.

### 4.3 Portfolio-Level Constraints

| Constraint | Limit | Rationale |
|---|---|---|
| Max concurrent positions | 5 | With 5 × 1% risk, a simultaneous wipeout costs 5%. With correlation spikes and gap risk, effective worst-case is 10–15% — painful but survivable. |
| Max sector exposure | 2 positions in the same GICS sub-industry (hard), 30% of deployed capital in the same GICS sector (soft) | **Changed from v2:** The hard constraint uses GICS sub-industry classification (robust to correlation estimation error) rather than relying solely on the noisy pairwise correlation matrix. The 30% sector capital limit serves as a secondary check. |
| Max correlated cluster | 3 positions with pairwise Ledoit-Wolf shrinkage-estimated 60d correlation > 0.6 | **Changed from v2:** Uses Ledoit-Wolf shrinkage estimator rather than sample correlation matrix. At 60 observations, sample correlation has standard error ~0.12 at r = 0.3 — too noisy for a binding constraint without shrinkage. The Ledoit-Wolf estimator reduces estimation error by ~40% at this sample size. |
| Max overnight exposure | 60% of account equity | Small-cap gap risk is highest overnight. Limiting total exposure reduces the worst-case overnight drawdown scenario. |

### 4.4 Risk Measurement: CVaR with Stress Augmentation

**Historical VaR is replaced with Conditional Value at Risk (CVaR / Expected Shortfall).**

```
Method:
1. Compute daily portfolio P&L using trailing 252 days of returns
2. Sort returns ascending
3. 95% CVaR = mean of all returns below the 5th percentile
   (not just the 5th percentile value — the average of the worst 5%)
4. 99% CVaR = mean of all returns below the 1st percentile
```

CVaR has two advantages over VaR: it is **subadditive** (the portfolio CVaR is always ≤ the sum of individual CVaRs, meaning diversification is properly credited), and it captures the *magnitude* of tail events rather than just their threshold. For fat-tailed small-cap distributions, the difference is material.

**Stress scenarios (run monthly):**

1. **Correlation shock:** All positions' returns set to their historical worst day simultaneously (simulating a correlation-1 event)
2. **Liquidity shock:** All positions gapped 2× their ATR against the trade direction at open (simulating an overnight gap with no ability to exit at the stop)
3. **Sector contagion:** The worst sector drawdown in the trailing 252 days applied to all positions in that sector simultaneously
4. **Catalyst compounding (new):** All positions with active catalysts (earnings within 5 days, pending regulatory events) gapped 3× ATR simultaneously, reflecting the higher conditional gap risk for signal-selected names

If any stress scenario produces a drawdown exceeding **15% of account equity**, the system reduces position count until the scenario passes the threshold.

### 4.5 Circuit Breakers

| Trigger | Action | Recovery |
|---|---|---|
| 5 consecutive losses | Reduce per-trade risk to 0.5% for next 10 trades | Automatic after 10 trades |
| 7% account drawdown, rolling 10-day window | Reduce max concurrent positions to 3 for 15 trading days | **Changed from v2:** Time-based recovery (15 days), then step to 4 positions for 10 days, then return to 5. This replaces the threshold-based recovery (recover to 4% drawdown) which created an asymmetric dead zone where the system was economically impaired for disproportionately long periods. |
| 12% account drawdown, rolling 20-day window | Halt all new positions. Manual review required. | Manual re-enable only after documented review of all losing trades, signal quality assessment, and explicit decision to continue |
| Single-day loss > 3% of account | No new positions for 2 trading days | Automatic |

The 5-consecutive-loss trigger has a ~3% probability of occurring in any 20-trade window at a 52% win rate. The 7% rolling drawdown represents a genuinely abnormal outcome rather than routine variance.

### 4.6 Take Profit / Stop Loss

```
Stop loss:  1.5 × ATR below entry (long) / above entry (short)
            2.25 × ATR for catalyst-proximate positions (Section 4.2)
            Never widened. Can be tightened via trailing stop after 1R profit.

Take profit: Tiered exit
            - 50% of position at 2R
            - Remaining 50%: trailing stop at 1.0 × ATR from high-water mark

Time stop:  If neither TP1 nor SL hit within 3 trading days, exit at next open.
```

The tiered exit captures the documented tendency of small-cap momentum to persist for 2–5 days post-catalyst while protecting against mean-reversion risk on the remaining position. The time stop prevents capital from being locked in directionless trades and ensures the system's holding period remains within the 1–3 day window where the information-lag thesis applies.

---

## 5. Transaction Cost Model

Transaction costs are the single largest threat to system viability. With the compressed edge estimate (Section 1.5), every basis point matters. The following model is used for all backtesting and live P&L calculation:

| Component | Estimate | Basis |
|---|---|---|
| Half-spread | 12–20 bps | Empirical measurement for stocks in the $500M–$2B universe with ≥ $5M daily volume and ≤ 40 bps spread (tighter universe filters improve this vs. v2) |
| Slippage (market impact) | 10–20 bps | Based on position size as fraction of daily midday volume; at 0.14% of daily volume, impact is minimal but nonzero |
| Commission | ~0 bps | Schwab zero-commission equities |
| Adverse selection | 15–25 bps | **Revised upward from v2's 5–10 bps.** Hasbrouck (2009) puts the adverse selection component at 30–50% of the effective half-spread for small caps. This is the *baseline* for random flow; Arconian's orders are explicitly informationally motivated (trading on unusual activity signals), which increases adverse selection cost. Conservative estimate reflects informed-flow premium. |
| Borrow cost (shorts only) | 15–50 bps | Applied per trade, annualized to holding period. Conservative default of 50 bps applied to all backtest short trades; actual cost logged during live trading. |

**Total estimated round-trip cost (longs): 75–130 bps per trade.**
**Total estimated round-trip cost (shorts): 90–180 bps per trade.**

**Conservative working assumption: 120 bps round-trip for longs, 150 bps for shorts.** Any signal or strategy that does not produce positive expectancy after these deductions is not traded, regardless of gross performance. This is substantially higher than v2's 100 bps assumption and reflects a more honest accounting of the costs faced by an informationally motivated retail trader in small-cap names.

**Cost monitoring:** The system logs intended entry price vs. actual fill price for every trade and decomposes realized cost into spread, impact, and residual (which proxies adverse selection). If realized costs exceed the model by > 30% on a rolling 20-trade basis (tightened from v2's 50% threshold), the cost model is updated and the minimum composite score for entry is raised to compensate.

### 5.1 Partial Fill Management

**New section.** With limit orders (entry at ask minus 5 bps buffer for longs, bid plus 5 bps for shorts), partial fills are inevitable in thin small-cap books.

| Fill Percentage | Action |
|---|---|
| < 50% of intended size within 5-minute window | Cancel remaining order, exit filled portion at market. Trade is logged as `partial_fill_abandoned`. |
| 50–80% of intended size | Accept position, adjust stop and take-profit proportionally to filled size. Log as `partial_fill_accepted`. |
| > 80% of intended size | Treat as full fill. |

**Partial fill bias monitoring:** Log the direction of the price at the time of partial fill cancellation (did it move toward or away from the entry?). Partial fills are biased — they're more likely when the stock is moving quickly in the trade direction (other buyers lifting offers), which means the system systematically undersizes its best trades. If partial fill frequency exceeds 25% of attempted entries over a 40-trade window, consider (a) widening the limit buffer to 10 bps, or (b) using IOC (immediate-or-cancel) orders for the full size.

---

## 6. Execution Architecture

### 6.1 System Topology

```
┌─────────────────────────────────────────────────────────────┐
│                    DATA COLLECTION LAYER                     │
│  Schwab API (intraday price/volume, options chains)          │
│  yfinance (EOD price, fundamentals, historical data)         │
│  Norgate Data (survivorship-bias-free historical universe)   │
│  EDGAR API (SEC filings: S-3, 8-K, 13F for corp actions)    │
│  StockTwits/Reddit API (retail attention signals)            │
│  Earnings calendar API (Earnings Whispers or equivalent)     │
│  macro_calendar.py (FOMC, CPI, NFP dates)                   │
└─────────────────┬───────────────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────────────┐
│                   SIGNAL ENGINE (Python)                      │
│  Universe manager: filters, delay scores, liquidity checks   │
│  Corporate action / earnings calendar exclusion engine       │
│  Signal computation: percentile ranks, multi-window          │
│  Retail attention penalty computation                        │
│  Composite scoring: IC-weighted, with options where avail    │
│  Directional confirmation: prior-day VWAP, return sign,      │
│    options flow, borrow availability (bearish)               │
│  Output: ranked candidate list with full feature vectors     │
└─────────────────┬───────────────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────────────┐
│              RISK ENGINE (Python)                             │
│  Position sizer: ATR-based, regime-adjusted, catalyst-aware  │
│  Portfolio checker: sub-industry, Ledoit-Wolf correlation,   │
│    exposure limits                                           │
│  CVaR calculator: 252-day, with 4 stress scenarios           │
│  Circuit breaker evaluator                                   │
│  Output: approved trades with exact share count & levels     │
└─────────────────┬───────────────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────────────┐
│            EXECUTION LAYER                                    │
│  Phase 1: Signal display + manual execution                  │
│  Phase 2: Schwab API semi-auto (approve/reject UI)          │
│  Phase 3: Schwab API fully automated with circuit breakers   │
│  Partial fill management (Section 5.1)                       │
│  All phases: full trade logging to signal_log + Zinniinae    │
└─────────────────┬───────────────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────────────┐
│            JOURNAL & ANALYTICS (Zinniinae)                    │
│  Trade log with P&L (pre-tax and after-tax), R-multiples     │
│  Signal outcome tracking (3-day forward returns)             │
│  Strategy-level expectancy dashboard                         │
│  IC decay monitoring per signal component                    │
│  Cost model validation (intended vs. actual fills)           │
│  Partial fill frequency and bias monitoring                  │
│  After-tax performance reporting                             │
└─────────────────────────────────────────────────────────────┘
```

### 6.2 The Algo System Is a Separate Application

Zinniinae is a Streamlit-based journal and analytics tool. The algo system requires capabilities that Streamlit cannot provide: autonomous scheduled execution, real-time data streaming, low-latency order management, and persistent background processes. The algo system is therefore a **standalone Python application** (CLI or lightweight web server) that communicates with Zinniinae through a shared database.

**Database configuration:** SQLite in **WAL (Write-Ahead Logging) mode** (`PRAGMA journal_mode=WAL;`). WAL mode allows concurrent reads during writes and dramatically reduces the write contention that would otherwise occur when the algo system writes signal log entries while Zinniinae writes trade annotations or manual overrides. This is a one-line configuration change that addresses the primary concurrency failure mode.

**Migration path:** If write contention remains an issue in Phase 3+ (automated execution with higher write frequency), migrate the shared data layer to PostgreSQL, which handles concurrent writes natively. For Phase 1–2, WAL-mode SQLite is sufficient.

Data flows:
- **Algo → Zinniinae:** Executed trades (paper or live) are written to Zinniinae's `trades` table. Signal events and outcomes are written to `signal_log`.
- **Zinniinae → Algo:** Watchlist and DD entries from Zinniinae can seed the algo's candidate universe. Manual overrides (block a ticker, force a review) are written to a shared `overrides` table. The corporate action blocklist is maintained in this table.

### 6.3 Schwab API Integration

#### 6.3.1 Tiered Scan Architecture

The full universe of 150–300 stocks, each requiring price + options chain data, would consume 600–1,500+ API calls per scan at Schwab's 120 requests/minute limit, extending scan time to 8–15 minutes — during which signals are going stale. v3 implements a **two-pass tiered scan:**

**Pass 1 — Price/Volume Screen (all names):**
- Pull price and volume data for all 150–300 names
- ~150–300 API calls, ~1.5–3 minutes
- Compute volume and return percentile ranks
- Score using volume, return, sector RS, delay, and retail attention signals (no options)
- Identify the **top 40 names** by Pass 1 composite score

**Pass 2 — Options Chain Enrichment (top 40 only):**
- Pull full options chains for the top 40 names
- ~80–120 API calls (accounting for pagination), ~1–2 minutes
- Compute options composite signal
- Recompute full composite score including options data

**Total scan time: 3–5 minutes.** Scans are scheduled at market open (9:35 AM ET, 5 minutes after open to allow opening auction to settle), midday (12:00 PM ET), and 30 minutes before close (3:30 PM ET).

**Tradeoff acknowledgment:** This architecture can miss a name with unremarkable price/volume but unusual options flow (options-only catalyst). The system monitors how often the options signal alone would have been the primary driver of a high composite score by computing hypothetical full-composite scores for a random 20% sample of Pass 1 non-qualifiers. If options-only catalysts are frequent (> 10% of eventual high-composite events), the Pass 1 cutoff is expanded to the top 60 names.

#### 6.3.2 Token Management

**Token validation frequency upgraded from v2's daily-at-6AM to before-every-scan.** The Schwab OAuth token expires weekly, and the weekly reset can fall on any day. A single lightweight API call (account balance query) validates the token before each scan. If validation fails:

1. Trigger immediate re-authentication flow
2. If re-auth succeeds, proceed with scan
3. If re-auth fails, emit an alert via Telegram (using existing Zinniinae integration) and halt new position entry
4. **Dead-man's switch (new):** If the system has not confirmed a successful scan within 60 minutes during market hours (9:30 AM – 4:00 PM ET), send an emergency alert. This protects against silent failures where the system process crashes entirely.

For open positions (Phase 3+), token failure during market hours triggers a graduated escalation:

1. **T+0 minutes:** Re-authentication attempt. If successful, resume normal operation.
2. **T+10 minutes:** If re-auth has failed, all positions' trailing stops are widened by 1 ATR as a buffer. Operator alerted via Telegram with full position summary.
3. **T+30 minutes:** If no operator acknowledgment received, **all open positions are closed at market.** This is a hard automatic rule, not discretionary. The rationale: in Phase 3+ with automated execution, a silent system crash with 5 open positions and no monitoring is a catastrophic-class risk. The cost of exiting at market (slippage, potentially unfavorable prices) is bounded and measurable; the cost of holding unmonitored positions through an overnight gap or adverse event is unbounded. The dead-man's switch prioritizes capital preservation over trade optimization.
4. **Post-closure:** System enters full halt. Manual review and explicit restart required before any new positions.

The 30-minute forced-exit threshold is calibrated to be long enough for an operator to respond to a Telegram alert during market hours but short enough to prevent unmonitored exposure through a material portion of the trading day. Operator acknowledgment resets the timer and allows manual management of open positions while the system issue is diagnosed.

#### 6.3.3 Order Execution (Phase 2+)

Limit orders only — never market orders.

- **Long entry:** Limit at current ask minus 5 bps buffer
- **Short entry:** Limit at current bid plus 5 bps buffer
- **Fill window:** 5 minutes. If the order doesn't fill within 5 minutes, it is canceled. Partial fill rules per Section 5.1 apply.
- **No chasing:** If the limit doesn't fill, the opportunity is logged as `signal_missed` with the price at cancellation time, enabling post-hoc analysis of whether missed signals outperform filled signals (which would indicate the limit buffer is too aggressive).

---

## 7. Data Collection & Signal Logging

### 7.1 The Signal Log: Foundation for Everything

The `signal_log` table is the most important data structure in the system. It records every candidate event — not just those that are traded — along with the full feature vector, contextual data, and the outcome measured 3 trading days later.

```sql
CREATE TABLE signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_timestamp DATETIME NOT NULL,       -- exact scan time (not just date)
    scan_type TEXT NOT NULL,                 -- 'open', 'midday', 'preclose'
    ticker TEXT NOT NULL,

    -- Feature vector (all continuous, all percentile ranks except delay)
    volume_pctile_20d REAL,
    volume_pctile_60d REAL,
    volume_pctile_120d REAL,
    return_pctile_60d REAL,
    options_composite REAL,                  -- NULL if no options data
    sector_rs_percentile REAL,               -- changed from z-score to percentile
    delay_score REAL,
    delay_score_as_of_signal REAL,           -- versioned: value active at signal time
    retail_attention_score REAL,             -- NEW: retail attention penalty input
    composite_score_raw REAL,                -- before retail penalty
    composite_score REAL,                    -- after retail penalty

    -- Signal weights active at signal time (for ML leakage prevention)
    weight_volume REAL,
    weight_return REAL,
    weight_options REAL,
    weight_sector_rs REAL,
    weight_delay REAL,

    -- Context
    spy_return_1d REAL,
    vix_level REAL,
    iwm_return_10d REAL,                     -- NEW: small-cap regime context
    iwm_return_20d REAL,                     -- NEW
    sector_etf TEXT,
    market_cap_mm REAL,
    avg_daily_dollar_vol REAL,
    midday_dollar_vol REAL,                  -- NEW: midday liquidity measure
    bid_ask_spread_bps REAL,
    atr_20 REAL,

    -- Event context (NEW)
    days_to_next_earnings INTEGER,
    days_since_last_earnings INTEGER,
    earnings_proximity_tag TEXT,              -- 'excluded', 'post_earnings_drift', 'normal'
    corporate_action_flag BOOLEAN DEFAULT FALSE,
    corporate_action_type TEXT,              -- 'secondary', 'reverse_split', 'merger', etc.
    catalyst_flag BOOLEAN DEFAULT FALSE,     -- earnings within 5d, FDA, halt, etc.
    history_status TEXT DEFAULT 'full',       -- 'full' or 'short' (observation mode)

    -- Direction
    direction_signal TEXT,                   -- 'bullish', 'bearish', 'ambiguous'
    prior_day_vwap REAL,                     -- reference VWAP used in confirmation
    borrow_available BOOLEAN,                -- for bearish signals

    -- Outcome (filled asynchronously 3 days later)
    return_1d REAL,
    return_3d REAL,
    return_from_signal_time_3d REAL,         -- NEW: signal-time-to-signal-time return
    max_adverse_excursion REAL,
    max_favorable_excursion REAL,
    outcome_label INTEGER,                   -- +1, -1, 0 per triple-barrier (Section 8.2)

    -- Trade metadata (NULL if not traded)
    was_traded BOOLEAN DEFAULT FALSE,
    trade_id INTEGER REFERENCES trades(id),
    entry_price REAL,
    exit_price REAL,
    realized_pnl REAL,
    realized_pnl_after_tax REAL,             -- NEW
    realized_r_multiple REAL,
    slippage_bps REAL,
    cost_spread_bps REAL,                    -- decomposed cost component
    cost_impact_bps REAL,                    -- decomposed cost component
    cost_adverse_selection_bps REAL,         -- decomposed residual
    cost_borrow_bps REAL,                    -- for shorts
    partial_fill_flag TEXT,                  -- 'full', 'partial_accepted', 'partial_abandoned'
    fill_pct REAL                            -- percentage of intended size filled
);
```

**Key changes from v2:**
- `scan_timestamp` and `scan_type` replace the ambiguous `timestamp`, enabling IC analysis segmented by scan timing
- `return_from_signal_time_3d` measures the return from signal time to signal time + 3 days using intraday data, addressing the measurement inconsistency where signals generated at 10 AM and 3:30 PM had different effective holding periods
- Versioned signal weights and delay score (`*_as_of_signal`) prevent temporal leakage in ML training (Section 8.3)
- Retail attention, earnings proximity, corporate action, and catalyst flags are first-class columns
- Cost decomposition enables granular transaction cost model validation
- After-tax P&L is tracked alongside pre-tax

**Why log non-traded events:** The ML training set must include events that *could have been traded* but weren't, to avoid survivorship bias. If we only log events we acted on, the model learns from a biased sample (we tend to skip the ambiguous ones, which may be the majority).

### 7.2 Outcome Measurement

**Close-to-close returns (primary):** `return_3d` uses close-to-close returns from signal day to signal day + 3. This is the primary metric for IC calculation and performance reporting because it is unambiguous and reproducible.

**Signal-time-to-signal-time returns (secondary):** `return_from_signal_time_3d` uses intraday data to measure the return from the exact scan timestamp to the same time 3 trading days later. This is the more accurate measure of what the signal actually predicted, but requires intraday data availability. IC analysis is run on both measures; if they diverge significantly, scan-time-specific composite weights may be warranted.

**Intraday excursion data collection:** A scheduled daily task (4:15 PM ET, after market close) pulls the daily high and low for all active candidate events (those within their 3-day measurement window). This requires ~30–60 API calls per day (well within rate limits) and captures the data needed for MAE/MFE calculation. Data is stored in an `outcome_prices` table linked to the signal_log:

```sql
CREATE TABLE outcome_prices (
    signal_id INTEGER REFERENCES signal_log(id),
    date DATE NOT NULL,
    day_offset INTEGER NOT NULL,    -- 0, 1, 2, or 3 (signal day through day+3)
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (signal_id, day_offset)
);
```

### 7.3 Feature Versioning

To prevent temporal leakage in ML training, all slowly-updating features are **versioned at signal time:**

| Feature | Update Frequency | Versioning Requirement |
|---|---|---|
| Delay score | Monthly | Store the delay score that was active at the time of the signal event, not the retrospectively re-estimated value |
| Signal weights | Every 60 trading days | Store the IC-adjusted weights that were active at signal time |
| Universe membership | Monthly | Point-in-time universe reconstruction for any historical analysis |
| Retail attention data sources | Continuous | Log data source availability flags (some sources may have downtime) |

The signal_log columns `delay_score_as_of_signal`, `weight_volume`, `weight_return`, `weight_options`, `weight_sector_rs`, and `weight_delay` serve this purpose. Any ML model trained on historical data must use only these as-of-signal values, never retrospectively updated values.

### 7.4 Information Coefficient Tracking

Every 60 trading days, the system computes:

```
IC_signal = spearman_rank_correlation(signal_value_at_entry, return_3d)
```

for each signal component, across all logged events in the trailing 252-day window. The IC values are stored in an `ic_history` table and displayed on a monitoring dashboard in Zinniinae.

**IC segmentation (new):** IC is computed not just in aggregate but segmented by:
- Scan type (open / midday / pre-close)
- Earnings proximity (normal / post-earnings-drift)
- History status (full / short)
- Retail attention quartile

This segmentation identifies whether signals have differential predictive power across contexts, informing future refinements (e.g., scan-time-specific weights, earnings-adjusted scoring).

An IC below 0.02 for two consecutive measurement periods triggers a review of that signal's inclusion and weighting. The minimum weight floor (0.05) ensures no signal is permanently zeroed — it may recover in a different regime.

---

## 8. Machine Learning Roadmap (Phase 2)

### 8.1 Prerequisites

ML model training begins **no earlier than 12 months** after signal logging begins, and only if the following conditions are met:

1. **≥ 3,000 labeled events** in `signal_log` (targeting 5,000+). At an estimated 8–15 candidate events per day (conservative, reflecting the tighter universe), 3,000 events requires ~200–375 trading days.
2. **Positive rule-based expectancy.** The rule-based system (Sections 3–4) must demonstrate positive expectancy after costs over ≥ 100 live trades. If the rule-based system is not profitable, adding ML will not fix it — the signals themselves are the problem.
3. **Stable IC values.** At least 3 consecutive 60-day IC measurement periods showing IC > 0.02 for ≥ 3 signal components. If ICs are unstable or declining, the underlying signal structure is shifting and a model trained on historical data will not generalize.

### 8.2 Labeling: Triple-Barrier Method

The v1 proposal used fixed-threshold labeling ("did the stock move ≥ 2% in 3 days?"). López de Prado's *Advances in Financial Machine Learning* demonstrates that fixed-threshold labels ignore volatility and path dependency, producing noisy and regime-dependent training targets.

The revised system uses the **triple-barrier method** with a volatility-expansion correction:

```
For each signal event:

  Reference ATR = ATR_20 computed as of the day BEFORE the signal event
    (avoids contamination from the signal day's range, which is often
     an outlier in high-composite-score events)

  IF signal day's true range > 2 × Reference ATR:
    Effective ATR = signal day's true range
    (dynamically adjusts for volatility expansion events)
  ELSE:
    Effective ATR = Reference ATR

  Upper barrier: +2.0 × Effective ATR above entry (take-profit)
  Lower barrier: -1.5 × Effective ATR below entry (stop-loss)
  Vertical barrier: 3 trading days

  Label = +1 if upper barrier hit first
          -1 if lower barrier hit first
           0 if vertical barrier hit first (time expiry)
```

**Change from v2:** The ATR used for barrier placement is now the pre-signal-day ATR (not the ATR_20 that includes the signal day), with dynamic widening if the signal day itself represents a volatility expansion. This corrects the systematic bias where barriers calibrated to pre-expansion volatility are too tight, inflating the proportion of barrier touches and deflating time-expiry labels.

Both the original and adjusted barrier placements are logged for analysis, enabling post-hoc assessment of whether the correction improves label quality.

### 8.3 Model Architecture: Meta-Labeling First

Rather than training a model to predict direction (a hard problem requiring large datasets), the initial ML deployment uses **meta-labeling**: the rule-based system generates directional signals, and the ML model predicts only **whether to take the trade (1/0)**.

This is a substantially easier learning problem:
- The feature space is the same (full feature vector from `signal_log`, using only as-of-signal versioned values)
- The label is binary: did this rule-based signal result in a profitable trade?
- The model learns to filter out the worst signals rather than generating new ones
- Required sample size is lower

**Model selection and sample size thresholds:**

| Model | Minimum Sample Size | Realistic Timeline | Deployment Condition |
|---|---|---|---|
| Regularized logistic regression (L1/L2) | 700 events (Silvey & Liu 2024) | Month 12–14 | First and likely primary ML deployment; must clear 5% Sharpe improvement |
| LightGBM | **8,000+ events** (revised from v2's implicit 3,000) | **Month 36+ (Year 3)** | Only if logistic model has demonstrably plateaued; requires 3–4 years of data |

**LightGBM constraint rationale and timeline honesty:** At 3,000 samples with ~10 features and a noisy financial label, gradient-boosted trees overfit even with aggressive regularization. The walk-forward validation may show inflated metrics because the model captures noise patterns that recur in adjacent temporal folds. The 8,000-event minimum ensures sufficient data for the model's complexity.

**To be explicit about what this means in practice:** At 8–15 candidate events per day, 8,000 events requires 530–1,000 trading days — approximately 2–4 calendar years of continuous data collection. LightGBM is not a Phase 2 deliverable. It is a Phase 4/5 enhancement that becomes available in Year 3 at the earliest. The regularized logistic regression meta-labeler is the realistic ML model for the foreseeable future, and the system must be designed to succeed with it. LightGBM is included in the specification as a planned evolution path with explicit prerequisites, not as a near-term commitment.

If the logistic model demonstrates strong performance and the data accumulates on schedule, LightGBM deployment uses extremely conservative hyperparameters: `max_depth ≤ 4`, `num_leaves ≤ 15`, `min_data_in_leaf ≥ 100`, `learning_rate ≤ 0.01` with early stopping. The goal is a barely nonlinear model that captures interaction effects the logistic model cannot, not a deep ensemble.

### 8.4 Probability Calibration

**New section.** If the meta-label model's output probability is used for trade filtering or (eventually) for position sizing via fractional Kelly, the probabilities must be well-calibrated: a predicted 70% should win ~70% of the time.

- **Logistic regression:** Apply Platt scaling on a held-out calibration set within the walk-forward framework
- **LightGBM:** Apply isotonic regression calibration (LightGBM is notoriously poorly calibrated out of the box)
- **Monitoring:** Compute calibration curves (predicted probability vs. actual win rate in decile bins) monthly after deployment. If the calibration slope deviates > 20% from 1.0, re-fit the calibration layer

The distinction between a true 55% edge and an apparent 70% edge drives very different Kelly-optimal bet sizes. Uncalibrated probabilities will systematically over-allocate to marginal signals.

### 8.5 Validation: Purged Walk-Forward with Full Embargo

Standard k-fold cross-validation produces dangerously inflated performance estimates on financial time-series data due to temporal leakage. The system uses **purged walk-forward validation with extended embargo:**

```
For each fold:
  Train: all data before the test window, using ONLY as-of-signal feature values
  Purge: remove 5 trading days before the test window start
         (prevents label leakage from overlapping 3-day outcome windows)
  Embargo: remove 5 trading days after the test window end
           (increased from v2's 3 days to provide additional buffer
            against forward-looking feature bleeding)
  Test: the designated test window (60 trading days)

Walk the window forward in 60-day increments across the full dataset.
```

**Critical leakage prevention (new):** Features with long lookback windows (delay_score, IC-adjusted weights) must use only the value that would have been available at the time of the signal event. The delay score for an event in month 5 is the month-4 estimate (not the month-6 re-estimate). This is enforced by the signal_log versioning columns (`delay_score_as_of_signal`, `weight_*`). Any ML pipeline that reads from signal_log must use these columns exclusively, never the current values.

**Minimum performance threshold for deployment:** The meta-label model must demonstrate ≥ 5% improvement in net Sharpe ratio over the rule-based baseline across all walk-forward folds. If it doesn't clear this bar, it adds complexity without benefit and is not deployed.

---

## 9. Tax Efficiency & After-Tax Reporting

### 9.1 Tax Impact Assessment

All Arconian trades are held 1–3 days — 100% short-term capital gains. At a combined federal + California state marginal rate of approximately 45–50% for ordinary income, the system needs roughly double the pre-tax return to match the after-tax performance of a longer-duration strategy taxed at long-term capital gains rates (~23.8% federal + ~13.3% California = ~37% combined for LTCG, though still substantial).

**Pre-tax vs. after-tax Sharpe:**

A pre-tax Sharpe of 0.5 in a high-frequency short-term strategy translates to approximately 0.25–0.30 after tax. This is marginal on a risk-adjusted basis, and the opportunity cost of capital deployed in Arconian vs. a tax-efficient buy-and-hold allocation must be considered.

### 9.2 Structural Mitigation

**Primary recommendation: Operate Arconian in a Roth IRA at Schwab** if contribution room permits. All gains in a Roth IRA are tax-free, eliminating the tax drag entirely. This is the single highest-impact optimization available at the capital scales involved.

**Constraints of Roth IRA operation:**
- No margin trading (all positions must be fully funded with cash in the account)
- No short selling (bearish signals cannot be executed with direct short positions; would require put options as a substitute, with different cost and risk characteristics)
- Annual contribution limits ($7,000 for 2026 for under-50, subject to income phase-outs)
- Cannot withdraw contributions penalty-free until age 59½ (with Roth-specific exceptions)

**If Roth IRA is not viable** (insufficient balance, income phase-out, need for short selling), the system operates in a taxable account with the following adjustments:
- All performance reporting includes after-tax columns
- The success criteria (Section 11) are evaluated on after-tax expectancy
- Tax-loss harvesting is implemented: positions closed at a loss within 30 days of a winning trade in a correlated name trigger a wash sale review before the loss is booked
- The minimum viable edge threshold is increased to account for the tax wedge: a pre-tax edge of 50 bps translates to ~25 bps after tax, which may not justify the operational overhead

### 9.3 After-Tax Performance Tracking

The `realized_pnl_after_tax` column in signal_log computes:

```
after_tax_pnl = realized_pnl × (1 - effective_tax_rate)

Where effective_tax_rate:
  = 0.00 if operating in Roth IRA
  = 0.50 if operating in taxable account (conservative estimate)
  = actual marginal rate if specifically configured
```

Zinniinae's strategy-level dashboard displays both pre-tax and after-tax expectancy, Sharpe ratio, and cumulative P&L. The go/no-go decision at the end of Phase 1 is made on after-tax metrics.

---

## 10. Relationship to Zinniinae

Zinniinae remains the **journal, analytics, and monitoring layer**. The algo system is a separate Python application that shares a WAL-mode SQLite database with Zinniinae and uses it as the human interface for review, analysis, and oversight.

| Function | System |
|---|---|
| Signal generation & scoring | Arconian algo |
| Corporate action / earnings exclusion | Arconian algo |
| Retail attention penalty computation | Arconian algo |
| Risk calculation & position sizing | Arconian algo |
| Order execution (Phase 2+) | Arconian algo |
| Partial fill management | Arconian algo |
| Token validation & dead-man's switch | Arconian algo |
| Trade logging & P&L tracking (pre- and after-tax) | Zinniinae |
| Signal outcome tracking & IC monitoring (segmented) | Zinniinae (reads signal_log) |
| Strategy-level expectancy dashboard | Zinniinae |
| Cost model validation & decomposition | Zinniinae |
| Partial fill frequency & bias monitoring | Zinniinae |
| After-tax performance reporting | Zinniinae |
| DD & qualitative notes | Zinniinae |
| Watchlist management | Zinniinae (shared with algo universe) |
| Manual overrides & corporate action blocklist | Zinniinae → shared overrides table |
| Calibration curve monitoring (Phase 3) | Zinniinae |

---

## 11. Build Sequence & Milestones

### Phase 0: Foundation (Weeks 1–6)

**Objective:** Build the universe manager, signal computation engine, and logging infrastructure. No trading.

- Implement universe screener: market cap ($500M–$2B), daily volume (≥ $5M), midday liquidity (≥ $1.5M), spread (≤ 40 bps), options OI (≥ 1,500 across ≥ 4 strikes), trading history (≥ 60 days)
- Implement short availability filter (Section 2.2)
- Compute Hou-Moskowitz delay scores for entire universe with monthly versioning
- Build percentile-rank signal computation for volume (multi-window), return magnitude, sector RS (percentile rank, not z-score), and options composite with all safeguards (IV rank floor, trade size diversity filter, 200-contract minimum)
- Build retail attention penalty computation (StockTwits, Reddit, scanner flag, SI proxy)
- Implement composite scoring with configurable weights and retail penalty multiplier
- Integrate earnings calendar and corporate action detection (EDGAR API)
- Build signal_log table with full schema (Section 7.1) including versioned features, earnings/corporate action flags, scan timing, and cost decomposition columns
- Build outcome_prices table and daily collection task
- Implement observation mode for new universe entrants
- Acquire Norgate Data subscription for survivorship-bias-free historical analysis
- Configure SQLite WAL mode
- Begin logging all candidate events daily (even before any trading)

**Milestone:** System produces a daily ranked candidate list with full feature vectors across 3 scans. Manual inspection confirms: (a) high-composite events correspond to genuinely unusual activity, (b) earnings-proximate and corporate-action events are correctly excluded/tagged, (c) retail attention penalty is reducing scores for names already flagged by major scanners.

### Phase 1: Rule-Based Trading + Data Collection (Months 2–8)

**Objective:** Trade the rule-based system on paper, then with small live positions. Collect signal outcome data aggressively. **Determine whether the edge exists at tradeable magnitude.**

- Implement risk engine: ATR sizing with catalyst gap premium, Ledoit-Wolf correlation constraints, GICS sub-industry limits, CVaR with 4 stress scenarios, circuit breakers with time-based recovery
- Implement directional confirmation layer (prior-day VWAP, return sign, options flow, borrow availability)
- Implement cost model with revised adverse selection estimates (Section 5) and partial fill management (Section 5.1)
- Implement after-tax P&L tracking (Section 9.3)
- **Paper trade for ≥ 80 trading days** (minimum 40 round-trip trades, increased from v2's 30)
- Evaluate at 40 paper trades: Is gross expectancy > 150 bps per trade? Is net expectancy (after 120 bps cost model for longs, 150 bps for shorts) positive?
- If paper results are positive: begin live trading with **0.5% risk per trade** (half the target allocation) for the next 60+ trades
- If operating in taxable account: evaluate on after-tax expectancy
- Continuously log all candidate events (traded and non-traded) to signal_log
- Compute IC for each signal component every 60 days, segmented by scan type, earnings proximity, and retail attention quartile; adjust weights if warranted
- Monitor partial fill frequency and bias (Section 5.1)
- Monitor realized cost decomposition vs. model (Section 5)

**Milestone:** 100+ round-trip trades (paper + live), with documented:
- Pre-tax and after-tax expectancy
- Win rate and average R-multiple
- Per-signal IC history (aggregate and segmented)
- Transaction cost model accuracy (intended vs. actual, decomposed)
- Partial fill frequency and directional bias
- Circuit breaker activation frequency
- **Paper-to-live expectancy gap decomposition** (fill rate, slippage, latency, adherence, override drags)
- **Failure mode classification** (Mode A / B / C) with supporting evidence

**Go/no-go decision:** Continue to full risk allocation if after-tax net expectancy is positive, realized costs are within 130% of model, and ≥ 3 signal components show IC > 0.02 for the most recent measurement period.

#### Paper-to-Live Transition Framework

The gap between paper trading and live trading is larger than most practitioners expect. Execution timing, psychological interference with limit orders, the temptation to override the system during drawdowns, and the mechanical realities of order routing all degrade live performance relative to paper results. Phase 1 must formally distinguish between three failure modes:

**Mode A — "Edge doesn't exist."** Both paper and live performance are negative. The signals lack predictive power in the target universe. Action: system halt, fundamental thesis review.

**Mode B — "Edge exists but execution is destroying it."** Paper performance is positive but live performance is negative or substantially degraded. The signals work, but the translation from signal to filled trade introduces enough friction to consume the edge. Action: diagnose execution, do not abandon the thesis.

**Mode C — "Operator is the problem."** Paper performance is positive, live execution metrics (fill quality, slippage, latency) are within tolerance, but live P&L is degraded because of discretionary overrides — skipping signals during drawdowns, widening limits when nervous, overriding the system's direction call. Action: address behavioral discipline or accelerate the move to semi-automated execution (Phase 4).

To distinguish these modes, the system runs **parallel paper and live tracking** during the first 60 live trades:

| Metric | Paper Tracking | Live Tracking | Diagnostic |
|---|---|---|---|
| Signal-to-execution latency | Time from signal to hypothetical fill (assume fill at next available price after signal) | Time from signal to actual fill | If live latency > 3× paper, execution infrastructure or operator response time is the bottleneck |
| Fill rate | 100% (paper assumes all signals are filled) | Actual fill rate (full, partial, missed) | If live fill rate < 70%, limit order strategy is too aggressive for the universe |
| Slippage | Hypothetical: signal price vs. close-of-signal-bar price | Actual: intended entry vs. fill price | If live slippage > 2× paper, market impact or adverse selection is worse than modeled |
| Signal adherence | 100% (paper takes all qualifying signals) | Actual: fraction of qualifying signals that were submitted as orders | If adherence < 85%, operator is selectively skipping signals (Mode C indicator) |
| Override rate | 0% | Actual: fraction of trades where operator modified size, limit price, or direction vs. system recommendation | If override rate > 10%, operator is substituting judgment for system rules |
| Expectancy | Paper expectancy (hypothetical fills, model costs) | Live expectancy (actual fills, actual costs) | The gap between these two numbers, decomposed into the above components, identifies the failure mode |

**Decomposition formula:**

```
Expectancy_gap = Paper_expectancy - Live_expectancy

Decomposed as:
  = (fill_rate_drag)           # signals missed or partially filled
  + (slippage_drag)            # worse fills than paper assumed
  + (latency_drag)             # price moved between signal and execution
  + (adherence_drag)           # signals skipped by operator
  + (override_drag)            # operator modifications that degraded outcomes
  + (residual)                 # unexplained — may indicate paper model is unrealistic
```

If the expectancy gap is > 50% of paper expectancy and is concentrated in slippage_drag + latency_drag, the system is in **Mode B** and the fix is execution infrastructure (tighter integration with Schwab API, move to Phase 4 semi-automation). If the gap is concentrated in adherence_drag + override_drag, it's **Mode C** and the fix is behavioral (or acceleration to automation). If paper expectancy itself is negative, it's **Mode A** regardless of execution quality.

The parallel tracking continues for the first 60 live trades. After 60 trades, if the decomposition shows the gap is within tolerance (live expectancy ≥ 70% of paper expectancy), the paper tracking can be discontinued. If the gap exceeds tolerance, paper tracking continues until the root cause is addressed.

**Kill criteria:** If after-tax net expectancy is negative after 100 trades, or if realized transaction costs exceed the model by > 30% consistently, the system is paused for fundamental review. The thesis is not falsified (it may be a parameter, universe, or implementation problem), but capital is preserved while the diagnosis occurs. Maximum expected loss at this point: $1,500–$3,000 (100 trades × 0.5% risk × 50% loss rate × ~$5K–$6K average position, minus winners).

### Phase 2: Risk Engine Hardening + ML Data Prep (Months 8–14)

**Objective:** Refine risk management with live data. Prepare ML training dataset. Activate tiered risk sizing if data supports it.

- Implement all 4 stress tests with live calibration data (correlation shock, liquidity shock, sector contagion, catalyst compounding)
- Calibrate circuit breaker thresholds with live drawdown data
- Backfill signal_log outcome fields for all historical events, including signal-time returns
- Compute and store IC history, delay score time series, cost model accuracy
- **Validate tiered risk hypothesis:** Is the rank correlation between composite score decile and win rate statistically significant (p < 0.05) over the accumulated trades? If yes, activate tiered risk (0.75% / 1.0% / 1.25%). If not, continue with flat 1.0%.
- Begin exploratory data analysis on signal_log: which feature combinations predict winners? Which scan times have highest IC? Do earnings-adjacent signals behave differently?
- Compute triple-barrier labels on all historical signal_log events using the corrected ATR methodology (Section 8.2)

**Milestone:** 14+ months of signal_log data, 3,000+ labeled events, documented IC trends, validated cost model, tiered risk activation decision made.

### Phase 3: ML Overlay (Months 14–20)

**Objective:** Train and validate meta-labeling model. Deploy only if it clears performance threshold.

- Train regularized logistic regression meta-label model on triple-barrier labels, using only as-of-signal feature values
- Apply Platt scaling calibration
- Validate via purged walk-forward with 5-day embargo
- If logistic model clears 5% Sharpe improvement threshold: deploy as a filter on rule-based signals
- Monitor calibration curves monthly
- If logistic model plateaus AND 8,000+ events are available: train LightGBM with conservative hyperparameters, validate identically, apply isotonic regression calibration
- Monitor live performance for 60 days before trusting model at full allocation

**Milestone:** ML-filtered system demonstrates ≥ 5% Sharpe improvement over rule-based baseline in live trading, with well-calibrated probabilities.

### Phase 4: Semi-Automated Execution (Months 14–20, parallel with Phase 3)

**Objective:** Reduce manual execution overhead while maintaining human oversight.

- Build Schwab API order submission with limit-order logic and partial fill management
- Build approval UI: system presents trade with full rationale (composite score, signal breakdown, cost estimate, risk check results, earnings/corporate action clearance), human clicks approve/reject
- All circuit breakers operate autonomously (no human override for halts)
- Implement dead-man's switch and escalation protocols (Section 6.3.2)
- Log fill quality, slippage decomposition, and partial fills

**Milestone:** Median time from signal to order submission < 2 minutes (vs. manual execution). Fill quality within 10% of manual execution baseline.

### Phase 5: Full Automation (Month 20+, conditional)

**Objective:** Remove human from the execution loop. Human remains in the monitoring loop.

- Automated order submission subject to all risk engine constraints
- Automated position management (trailing stops, time stops, tiered exits)
- Automated partial fill management
- Daily summary report to operator (email or Zinniinae dashboard) including:
  - All trades executed with full rationale
  - Pre-tax and after-tax P&L
  - Cost model accuracy
  - Circuit breaker status
  - IC trend summary
  - Any anomalies (unusual partial fill rate, cost exceedances, token issues)
- Weekly manual review of all trades, signal quality, IC trends, cost model accuracy
- Monthly strategy review: Is the edge persisting? Are costs under control? Any regime changes? Has the retail attention landscape shifted?

**Prerequisites for Phase 5:** ≥ 200 live trades with positive after-tax net expectancy, all 4 stress tests passing, ML model (if deployed) stable for ≥ 3 months with calibrated probabilities, no 12% circuit breaker events in trailing 60 days.

---

## 12. Success Criteria & Honest Expectations

### What "Working" Looks Like

After 8 months of live trading (Phase 1 complete):

| Metric | Pre-Tax Target | After-Tax Target (Taxable) | After-Tax Target (Roth) |
|---|---|---|---|
| Net expectancy per trade | > 20 bps | > 10 bps | > 20 bps |
| Win rate | 47–52% | Same | Same |
| Average winner | 1.3–1.8R | Same | Same |
| Average loser | 0.8–1.0R | Same | Same |
| Profit factor | > 1.15 | > 1.08 | > 1.15 |
| Maximum drawdown | < 12% | < 12% | < 12% |
| Sharpe ratio (annualized) | > 0.5 | > 0.25 | > 0.5 |

These targets are **deliberately modest** and reflect the post-publication haircut (Section 1.2) and revised cost estimates (Section 5). A Sharpe of 0.5 pre-tax is below most institutional thresholds but represents a meaningful edge for a retail system. The after-tax Sharpe of 0.25 in a taxable account is marginal — this is why the Roth IRA recommendation (Section 9.2) is structurally important.

The goal is not to get rich — it is to demonstrate a **replicable, positive-expectancy process** that can be refined and potentially scaled.

### What Failure Looks Like

| Failure Signal | Threshold | Implication |
|---|---|---|
| After-tax net expectancy ≤ 0 after 100 trades | First kill criterion | Signals don't survive transaction costs + taxes |
| Realized costs consistently > 130% of model | 20-trade rolling window | Target universe is too illiquid or adverse selection is worse than estimated |
| IC for all signal components < 0.02 for 2+ consecutive periods | 120 trading days | Signals have no predictive power in the current regime |
| Maximum drawdown > 15% | Any point | Risk model has failed through mis-calibration or unmodeled regime |
| Partial fill rate > 40% of attempted entries | 40-trade window | Limit order strategy is incompatible with the target universe's liquidity |
| Retail attention penalty consistently > 0.5 for high-composite names | Ongoing | The "neglected" universe is no longer neglected; thesis may be structurally impaired |

Failure is not shameful. **97% of retail short-term traders lose money** (Barber et al. 2014; Chague et al. 2020). The system is designed to detect failure quickly (within 100 trades) and preserve capital (circuit breakers, conservative sizing, half-Kelly Phase 1 allocation) so that failure is a **$1,500–$3,000 tuition payment** rather than a $15,000 catastrophe.

### The Learning Outcome

Regardless of P&L outcome, the system produces:
- A 12+ month dataset of labeled signal events across 150–300 small-cap stocks, with full feature vectors, earnings/corporate action context, retail attention data, and transaction cost decomposition
- Empirical IC measurements for each signal component, segmented by scan time, earnings proximity, and retail attention regime
- A validated (or invalidated) transaction cost model for small-cap swing trading, with decomposed cost components
- A complete, auditable trade journal with full provenance and after-tax performance tracking
- Practical experience with systematic trading infrastructure, risk management, execution, and ML pipeline design
- A survivorship-bias-free historical dataset (via Norgate) for future research

This dataset and experience base has value independent of the trading P&L. It forms the foundation for iteration — adjusting the universe, refining signals, testing new strategies — or for pivoting to a different approach entirely with the benefit of hard-won empirical knowledge.

---

## Appendix A: Glossary of Key Terms

| Term | Definition |
|---|---|
| **ATR** | Average True Range — a volatility measure computed over 20 trading days |
| **Composite score** | The weighted sum of all signal components, penalized by retail attention |
| **CVaR** | Conditional Value at Risk (Expected Shortfall) — the average loss in the worst X% of scenarios |
| **IC** | Information Coefficient — Spearman rank correlation between a signal and subsequent returns |
| **Ledoit-Wolf** | A shrinkage estimator for covariance/correlation matrices that reduces estimation error |
| **MAE / MFE** | Maximum Adverse / Favorable Excursion — the worst/best intraday move against/for a position |
| **Meta-labeling** | ML technique where the model predicts whether to take a trade generated by a rule-based system |
| **Percentile rank** | The fraction of trailing observations at or below the current value; distribution-free |
| **Price delay** | Hou-Moskowitz metric: fraction of market-related return variation explained by lagged vs. contemporaneous returns |
| **R-multiple** | The ratio of profit (or loss) to the initial risk (stop distance) on a trade |
| **Triple barrier** | Labeling method using take-profit, stop-loss, and time-expiry barriers scaled by volatility |
| **WAL mode** | Write-Ahead Logging — SQLite journaling mode enabling concurrent reads during writes |

## Appendix B: Data Source Requirements

| Source | Purpose | Cost | Criticality |
|---|---|---|---|
| Schwab API | Live price, volume, options chains, order execution | Free (account holder) | Critical |
| yfinance | EOD price, fundamentals, historical data | Free | High |
| Norgate Data | Survivorship-bias-free historical universe | ~$500/year | Critical for any backtest |
| EDGAR API | SEC filings (S-3, 8-K, 13F) for corporate actions | Free | High |
| StockTwits API | Social mention velocity | Free tier | Medium |
| Reddit API | Social mention velocity | Free tier / third-party | Medium |
| Earnings calendar | Earnings dates for exclusion zones | Free (Earnings Whispers, Yahoo Finance) | High |
| Databento | Historical tick data for ML training (Phase 2) | Variable | Medium (Phase 2 only) |

## Appendix C: Change Log from v2

| Section | Change | Rationale |
|---|---|---|
| 1.2 | Added 50% post-publication haircut on all academic effect sizes | McLean & Pontiff (2016): anomalies decay ~58% post-publication |
| 1.3 | Added post-2020 microstructure regime analysis | Retail scanners, 0DTE, zero-commission trading compress delay windows |
| 1.5 | Revised edge estimates downward; raised minimum viable gross edge to 150 bps | Haircut + revised costs |
| 2.1 | Raised market cap floor to $500M, added midday liquidity filter, tightened spread to 40 bps, raised options OI to 1,500 with 4-strike distribution, added 60-day history requirement | Addresses liquidity mirage, noisy options, cold-start problems |
| 2.2 | Added short availability filter | Bearish trades impossible without borrow; unmodeled cost |
| 2.4 | Added survivorship-bias-free data requirement (Norgate) | Any backtest without delisted names is biased |
| 3.2.3 | Reduced options weight from 0.25 to 0.15; added 200-contract minimum, trade size diversity filter, IV rank safeguard | Pan & Poteshman (2006): public options data has degraded predictive power |
| 3.2.4 | Changed Sector RS from z-score to percentile rank | Internal inconsistency: system argued against z-scores then used one |
| 3.2.6 | Added retail attention penalty | Post-2020 microstructure: scanner-flagged names have compressed delay |
| 3.4 | Changed VWAP reference from intraday to prior-day closing VWAP | Scan-timing dependency: intraday VWAP varies by check time |
| 3.5 | Added earnings calendar and corporate action integration | Massive false positive source without earnings/corp action awareness |
| 3.6 | Added observation mode for new universe entrants | Cold-start: <120 days of history produces noisy percentile ranks |
| 4.2 | Added catalyst gap risk premium (1.5× ATR multiplier) and tiered risk (deferred to Phase 2) | Signal-selected names have higher conditional gap risk; flat sizing leaves money on table |
| 4.2 | Added IWM-based regime detector alongside VIX | Small-cap selloffs diverge from VIX; Russell 2000 captures them |
| 4.3 | Changed correlation constraint to Ledoit-Wolf shrinkage estimator; added GICS sub-industry hard constraint | Sample correlation at n=60 has SE ~0.12; too noisy for binding constraint |
| 4.4 | Added catalyst compounding stress scenario | Standard stress scenarios understate risk for catalyst-proximate positions |
| 4.5 | Changed circuit breaker recovery to time-based (15 days) | Threshold-based recovery (7% → 4%) creates asymmetric dead zone |
| 5 | Raised adverse selection to 15–25 bps; raised working cost assumption to 120 bps (longs), 150 bps (shorts) | Hasbrouck (2009) + informed-flow premium; v2 underestimated by 10–20 bps |
| 5.1 | Added partial fill management rules and bias monitoring | Partial fills are inevitable with limit orders in thin books; biased toward undersizing best trades |
| 6.2 | Added SQLite WAL mode; PostgreSQL migration path | Write contention between algo and Zinniinae |
| 6.3.1 | Implemented two-pass tiered scan | API rate limits make full-universe options scans too slow (8–15 min) |
| 6.3.2 | Token validation before every scan; dead-man's switch | Weekly token expiry mid-session causes silent scan failures |
| 7.1 | Expanded signal_log schema with scan timing, versioned features, earnings/corp action flags, cost decomposition, after-tax P&L | Addresses measurement inconsistency, ML leakage, false positives, tax reporting |
| 7.2 | Added signal-time-to-signal-time return measurement and outcome_prices table | Different scan times create different effective holding periods |
| 7.3 | Added feature versioning requirements | Prevents temporal leakage in ML training |
| 7.4 | Added segmented IC analysis | Identifies context-dependent signal quality differences |
| 8.2 | Corrected triple-barrier ATR to use pre-signal-day value with dynamic widening | Signal day's range contaminates ATR; barriers too tight for expansion events |
| 8.3 | Raised LightGBM minimum to 8,000+ events; added conservative hyperparameter constraints; **added explicit timeline honesty (Year 3+)** | 3,000 samples insufficient for tree-based model; overfitting risk; logistic regression is the realistic ML model |
| 8.4 | Added probability calibration requirement (Platt scaling / isotonic regression) | Uncalibrated probabilities cause systematic over-allocation |
| 8.5 | Extended embargo to 5 days; enforced as-of-signal feature usage | Longer lookback features leak through 3-day embargo |
| 9 | Added full tax efficiency section with Roth IRA recommendation and after-tax reporting | After-tax Sharpe may not justify the effort without structural tax optimization |
| 11 | Revised success criteria downward; added after-tax targets; expanded failure signals | Reflects compressed edge, higher costs, and tax reality |
| 3.2.3 | Added accelerated IC recalibration for options signal (30-day vs. 60-day cycle) | Options weight of 0.15 may be too conservative; faster feedback loop lets it earn back to 0.20+ if warranted |
| 6.3.2 | Expanded dead-man's switch with graduated escalation: 10-min widen stops → 30-min force close all positions at market → full halt | "Send an alert" is incomplete; unacknowledged system crash with open positions is catastrophic-class risk |
| 11 (Phase 1) | Added paper-to-live transition framework with parallel tracking, expectancy gap decomposition, and three failure mode classifications (A/B/C) | Paper-to-live gap is larger than expected; system must formally distinguish "edge doesn't exist" from "execution is destroying the edge" from "operator is the problem" |
