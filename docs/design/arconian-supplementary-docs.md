# Arconian Implementation Supplements
### Three Pre-Architecture Documents — April 2026

---

# Document 1: Data Source Contracts

Each external dependency is specified with exact endpoints, authentication, rate limits, expected response schemas, failure modes, and fallback behavior. These contracts define the adapter interface each module must implement.

---

## 1.1 Schwab API (via `schwab-py`)

**Library:** `schwab-py` (unofficial Python wrapper by Alex Golec)
**Base URL:** `https://api.schwabapi.com/v1/`
**Auth:** OAuth 2.0 — access token (30-min TTL), refresh token (7-day TTL)
**Global Rate Limit:** ~120 requests/minute for Market Data; 2–4 trade requests/sec for Orders

### Endpoints Used

#### A. Price Quotes (Batch)
- **Method:** `client.get_quotes(symbols)` — accepts comma-separated list
- **Max per call:** ~100 symbols in a single request
- **Response fields used:** `lastPrice`, `totalVolume`, `bidPrice`, `askPrice`, `highPrice`, `lowPrice`, `closePrice`, `52WkHigh`, `52WkLow`, `regularMarketLastPrice`
- **Rate cost:** 1 API call per batch of up to 100 symbols
- **Arconian usage:** Pass 1 of tiered scan — pull quotes for full universe (150–300 names) in 2–3 batched calls

#### B. Price History (Per Symbol)
- **Method:** `client.get_price_history_every_day(symbol)` or `get_price_history_every_minute(symbol)`
- **Daily bars:** Up to 15 years lookback
- **Intraday (1-min/5-min):** Up to 6 months lookback for equities
- **Response fields:** `open`, `high`, `low`, `close`, `volume`, `datetime`
- **Rate cost:** 1 API call per symbol per request
- **Arconian usage:**
  - Daily bars: ATR computation, trailing percentile ranks, Hou-Moskowitz delay regression (weekly aggregation from daily data)
  - 5-min bars: VWAP computation (prior day closing VWAP), midday volume measurement (10:30–14:30 ET)
  - **VWAP computation note:** Schwab does NOT expose a "closing VWAP" field. Arconian must compute it from intraday bars: `VWAP = Σ(price_i × volume_i) / Σ(volume_i)` using all 5-min bars for the trading day. This requires pulling 5-min history for the prior trading day for every universe member — **one API call per symbol per day**. At 150–300 names, this is 150–300 calls. Schedule this at 4:15 PM ET daily (after close) alongside the outcome_prices collection task, NOT during scan windows.

#### C. Options Chains (Per Symbol)
- **Method:** `client.get_option_chain(symbol, contract_type='ALL', strike_count=None, from_date=..., to_date=...)`
- **Pagination:** A single call returns the full chain for the specified date range. For near-term expirations (next 2 months), this is typically 1 call per symbol. Very active names with many expirations may require `from_date`/`to_date` windowing.
- **Response structure:** Nested dict keyed by expiration date, then by strike. Each contract includes: `totalVolume`, `openInterest`, `volatility` (IV), `putCall`, `strikePrice`, `expirationDate`, `bid`, `ask`, `last`, `delta`, `gamma`, `theta`, `vega`
- **Rate cost:** 1–2 API calls per symbol (depends on chain depth)
- **Arconian usage:** Pass 2 of tiered scan — top 40 names. Extract `totalVolume`, `openInterest`, `volatility` per contract. Aggregate across expirations for: total call vol, total put vol, total OI, weighted-average IV, distinct trade sizes (via volume distribution across strikes), strike distribution count.
- **OI/strike distribution check:** Count distinct strikes with OI > 0 to verify the ≥4-strike requirement. Sum all OI across expirations to verify ≥1,500 aggregate OI.

#### D. Account Info / Token Validation
- **Method:** `client.get_account_numbers()` — lightweight call that validates token
- **Response:** Array of `{accountNumber, hashValue}` objects
- **Rate cost:** 1 API call
- **Arconian usage:** Pre-scan token validation (Section 6.3.2 of whitepaper)

#### E. Order Placement (Phase 2+)
- **Method:** `client.place_order(account_hash, order_spec)`
- **Order types:** Limit, stop, stop-limit. Use `schwab.orders.equities` helpers.
- **Rate limit:** 2–4 requests/sec (separate from data rate limit)
- **Response:** HTTP 201 on success; order ID in `Location` header. Extract via `schwab.utils.Utils.extract_order_id(response)`
- **Arconian usage:** Limit orders per Section 6.3.3. Entry at ask − 5 bps (longs), bid + 5 bps (shorts).

#### F. Order Status / Cancel
- **Method:** `client.get_orders_for_account(account_hash)`, `client.cancel_order(account_hash, order_id)`
- **Arconian usage:** Partial fill monitoring (5-min fill window), order cancellation for unfilled orders

### Authentication Flow
```
1. First-time: browser-based OAuth flow via schwab.auth.easy_client()
   → Produces token.json (access_token + refresh_token)
2. Ongoing: schwab-py auto-refreshes access_token using refresh_token
3. Refresh token expires after 7 days → requires manual re-auth via browser
4. Dead-man's switch: if get_account_numbers() fails → trigger re-auth → 
   if re-auth fails → Telegram alert → escalation per Section 6.3.2
```

### Failure Modes & Fallbacks
| Failure | Detection | Fallback |
|---|---|---|
| Token expired (401) | HTTP 401 on any call | Auto-refresh via schwab-py; if refresh fails, Telegram alert + halt |
| Rate limit hit (429) | HTTP 429 + `Retry-After` header | Exponential backoff with jitter; log rate limit event; if persistent, reduce Pass 1 batch size |
| Schwab API outage (500/503) | HTTP 5xx | Retry 3x with 10s backoff; if all fail, skip scan, log as `scan_failed`, Telegram alert |
| Malformed response | JSON parse error or missing expected fields | Log raw response, skip symbol, continue scan |
| Weekend/holiday | API returns stale data or no data | Detect via market calendar (see `trading_calendars` package); skip scans on non-trading days |
| Options chain empty for listed name | Response returns empty `callExpDateMap`/`putExpDateMap` | Set `options_composite = NULL`; redistribute weight |

### Critical Constraint: Windows Dependency
`schwab-py` runs on any OS, but Norgate Data Updater (NDU) is **Windows-only**. If the system runs on Linux/macOS, Norgate must run on a separate Windows machine or Windows VM with shared filesystem/network access. Design the data adapter layer with this in mind.

---

## 1.2 SEC EDGAR (Free Public API)

**Primary approach: SEC's free EFTS (Electronic Full-Text Search) endpoint + Company Filings API**
**Alternative: sec-api.io (paid, $0.01/request, better structured data)**

### Free Tier (Preferred — No API Key Required)

#### A. Company Filings Lookup
- **Endpoint:** `https://data.sec.gov/submissions/CIK{cik_padded}.json`
- **Rate limit:** 10 requests/second (with proper User-Agent header)
- **User-Agent requirement:** Must include contact email, e.g.: `Arconian/1.0 (you@example.com)`
- **Response:** JSON with `recentFilings` array containing `form`, `filingDate`, `accessionNumber`, `primaryDocument` for recent filings. Historical filings in separate paginated files.
- **Arconian usage:** For each universe member, pull recent filings and scan for form types: `S-3`, `S-3/A` (secondary offerings), `8-K` (material events), `SC 13D` (activist stake), `F-4` (SPAC mergers)

#### B. EDGAR Full-Text Search (EFTS)
- **Endpoint:** `https://efts.sec.gov/LATEST/search-index?q={query}&dateRange=custom&startdt={start}&enddt={end}&forms={form_type}`
- **Rate limit:** 10 requests/second (same as above)
- **Max results:** 10,000 per query (paginate with `from` parameter, 100 results per page)
- **Arconian usage:** Search for specific corporate action keywords within 8-K filings:
  - Reverse split: search `"reverse stock split"` in 8-K filings by CIK
  - Secondary offering: search form type `S-3` by CIK
  - Merger/acquisition: search 8-K Item 1.01 ("Entry into a Material Definitive Agreement")

#### C. 8-K Item Detection
8-K filings contain structured "items" that indicate the type of event. The free API returns the filing text, not parsed items. Detection requires:
```python
# Regex patterns for 8-K item detection
ITEM_PATTERNS = {
    'merger_acquisition': r'Item\s+1\.01',      # Material Definitive Agreement
    'bankruptcy': r'Item\s+1\.03',               # Bankruptcy/Receivership
    'mine_safety': r'Item\s+1\.04',              # Mine Safety
    'material_impairment': r'Item\s+2\.06',      # Material Impairment
    'reverse_split': r'(?:reverse\s+(?:stock\s+)?split)',  # Text search, not item
    'ticker_change': r'Item\s+8\.01',            # Other Events (often ticker changes)
    'delisting': r'Item\s+3\.01',                # Delisting notice
}
```
This is imperfect — Item 8.01 is a catch-all for "Other Events" that includes ticker changes but also many irrelevant disclosures. The manual blocklist (Section 3.5.2 of whitepaper) is the safety net.

### Paid Alternative: sec-api.io
- **Endpoint:** `https://api.sec-api.io` (Query API) and `https://api.sec-api.io/full-text-search` (Full-Text API)
- **Rate:** Starts at $0.01/request; includes structured `formType`, `items` fields
- **Advantage:** Pre-parsed 8-K items, cleaner data, real-time filing stream via WebSocket
- **Recommendation:** Start with free EDGAR API. If false positive rate from corporate action detection exceeds 15% of flagged events (measured during Phase 0), migrate to sec-api.io. Budget: ~$50/month at expected query volume.

### CIK Mapping
Arconian needs to map tickers to CIK numbers. The SEC provides a complete mapping:
- **Endpoint:** `https://www.sec.gov/files/company_tickers.json`
- **Cache locally:** Refresh weekly. The file is ~2MB and contains all active tickers mapped to CIK.

### Failure Modes & Fallbacks
| Failure | Detection | Fallback |
|---|---|---|
| Rate limited (429) | HTTP 429 | Back off to 5 req/s for 60 seconds |
| EDGAR outage | HTTP 5xx or timeout | Skip corporate action check for this scan; flag affected tickers as `corp_action_check_failed` in signal_log |
| CIK mapping miss (ticker not found) | Ticker not in company_tickers.json | Log as `cik_not_found`; manually investigate; likely a very recent listing |
| 8-K item misparse | Regex doesn't match actual item structure | Manual blocklist catches critical cases; log all parsed items for audit |

---

## 1.3 StockTwits API

**Base URL:** `https://api.stocktwits.com/api/2/`
**Auth:** OAuth optional; unauthenticated access available for read-only
**Rate limit:** 200 requests/hour (unauthenticated), 400 requests/hour (authenticated)

### Endpoint Used

#### Symbol Stream
- **URL:** `GET https://api.stocktwits.com/api/2/streams/symbol/{SYMBOL}.json`
- **Response:** JSON with `messages` array (max 30 per request), each containing `body`, `created_at`, `sentiment` (if tagged: `bullish`/`bearish`/`null`)
- **Pagination:** Use `max` parameter with message ID for older messages
- **Key fields for Arconian:** Count of messages in trailing 24 hours, sentiment distribution

### Rate Budget for Arconian
At 200 req/hr unauthenticated (conservative), scanning 300 symbols requires 300 calls. This **exceeds the hourly limit**. Solutions:

1. **Batch across scans:** Don't scan all symbols every scan. Scan only the Pass 1 top 40 candidates per scan (40 calls × 3 scans/day = 120 calls/day — well within limits)
2. **Register an OAuth app:** Doubles the limit to 400/hour
3. **Cache mention counts:** Social velocity is a trailing 24-hour metric. Cache previous results and only refresh for Pass 1 qualifiers.

### Alternative: StockTwits Trending Endpoint
- **URL:** `GET https://api.stocktwits.com/api/2/trending/symbols.json`
- **Response:** Top 30 trending symbols with `watchlist_count`
- **Rate cost:** 1 call
- **Arconian usage:** Quick check if any universe member is currently trending → instant `scanner_flag = 1.0`

### Failure Modes & Fallbacks
| Failure | Detection | Fallback |
|---|---|---|
| Rate limited (429) | HTTP 429 | Set `social_velocity = 0.0` (no penalty) for unscanned symbols; log as `stocktwits_rate_limited` |
| API deprecated/removed | HTTP 404 or persistent errors | StockTwits has historically been unreliable. Fallback: set social_velocity component to 0.0, re-weight remaining retail attention components. This is a Medium-criticality source; system is designed to degrade gracefully. |
| Message count = 0 | Valid response with empty messages | Legitimate — stock has no StockTwits activity. social_velocity = 0th percentile = 0.0 |

---

## 1.4 Reddit API (via PRAW)

**Library:** `praw` (Python Reddit API Wrapper)
**Base URL:** `https://oauth.reddit.com/`
**Auth:** OAuth 2.0 (script-type app) — register at reddit.com/prefs/apps
**Rate limit:** 60 requests/minute (OAuth authenticated); 10-minute rolling window

### Endpoint Used

#### Subreddit Search
- **URL:** `GET /r/{subreddit}/search?q={ticker}&sort=new&restrict_sr=true&t=day`
- **Target subreddits:** `wallstreetbets`, `stocks`, `pennystocks`, `smallstreetbets`, `options`
- **Response:** Listing of posts matching the query, with `created_utc`, `score`, `num_comments`
- **Max per request:** 100 items
- **Arconian usage:** Count posts mentioning `$TICKER` or `TICKER` (with word boundary) in trailing 24 hours across target subreddits

### Rate Budget
5 subreddits × 40 symbols (Pass 1 top 40) = 200 calls per scan. At 60/min, this takes ~3.3 minutes. Acceptable if run asynchronously during the Pass 1 → Pass 2 transition.

**Optimization:** Use a single search across `r/all` with `q=$TICKER` instead of per-subreddit queries. This reduces to 40 calls per scan (40 seconds). Filter results by subreddit in post-processing.

### Failure Modes & Fallbacks
| Failure | Detection | Fallback |
|---|---|---|
| Rate limited (429) | `Retry-After` header or HTTP 429 | Respect `Retry-After`; for remaining symbols, set `reddit_mentions = 0` (no penalty) |
| OAuth token expired | HTTP 401 | PRAW handles refresh automatically; if persistent, re-authenticate |
| API access revoked | HTTP 403 | Reddit has been tightening API access. If this happens, fall back to r/all search only or use a third-party aggregator (e.g., Quiver Quantitative social sentiment API). Set reddit component to 0.0 and re-weight. |
| Subreddit private/banned | HTTP 403 on specific subreddit | Skip that subreddit; log which subreddits are unavailable |

---

## 1.5 yfinance (EOD Data & Fundamentals)

**Library:** `yfinance` Python package
**Base URL:** Scrapes Yahoo Finance (no official API; uses undocumented endpoints)
**Auth:** None
**Rate limit:** Unofficial; ~2,000 requests/hour is generally safe; aggressive scraping triggers IP blocks

### Endpoints Used (via yfinance methods)

#### A. Historical Daily Prices
- **Method:** `yf.Ticker(symbol).history(period="1y")` or `yf.download(symbols, period="1y")`
- **Response:** DataFrame with `Open`, `High`, `Low`, `Close`, `Volume`, `Dividends`, `Stock Splits`
- **Arconian usage:** Supplementary data source for daily prices when Schwab API is unavailable or for non-market-hours batch processing. Also used for sector ETF price data (XLK, XLF, etc.) for Sector RS computation.

#### B. Ticker Info (Fundamentals)
- **Method:** `yf.Ticker(symbol).info`
- **Key fields:** `sector`, `industry`, `marketCap`, `shortPercentOfFloat`, `sharesOutstanding`, `forwardPE`
- **Arconian usage:**
  - `sector` → GICS sector mapping for Sector RS signal and portfolio constraint
  - `marketCap` → Universe filter validation
  - `shortPercentOfFloat` → Short interest crowding component of retail attention penalty
  - **IMPORTANT:** yfinance sector names do NOT exactly match GICS sector names. A mapping table is required (see Document 3, Section 3.4).

#### C. Earnings Calendar
- **Method:** `yf.Ticker(symbol).calendar` or `yf.Ticker(symbol).earnings_dates`
- **Response:** Dict with `Earnings Date` (list of dates), `Earnings Average`, `Revenue Average`
- **Arconian usage:** Populate `days_to_next_earnings` and `days_since_last_earnings` in signal_log
- **Reliability caveat:** yfinance earnings dates are scraped from Yahoo Finance and are occasionally wrong or missing. Cross-reference with Earnings Whispers or Alpha Vantage for critical dates.

### Failure Modes & Fallbacks
| Failure | Detection | Fallback |
|---|---|---|
| IP throttled | ConnectionError or empty response | Implement 2s delay between calls; rotate User-Agent; for batch downloads use `yf.download()` which is more efficient |
| Data quality issue (split-adjusted error) | Sudden >50% price change not matching Schwab data | Always prefer Schwab API data for live signals; yfinance is secondary |
| Earnings date missing | `calendar` returns empty or NaT | Fall back to Earnings Whispers scrape or manual lookup; flag as `earnings_date_unknown` |
| Yahoo Finance endpoint changes | yfinance raises new exceptions | yfinance updates frequently; pin version in requirements.txt; monitor GitHub issues |

---

## 1.6 Norgate Data (Historical, Survivorship-Bias-Free)

**Library:** `norgatedata` Python package
**Platform:** Windows-only (requires Norgate Data Updater running locally)
**Auth:** Subscription-based (data stored locally in proprietary database)
**Rate limit:** Local database queries — effectively unlimited speed
**Cost:** ~$500/year for US Equities (Platinum tier required for historical index constituents and delisted stocks)

### Key Functions

#### A. Price Time Series
```python
import norgatedata
pricedata = norgatedata.price_timeseries(
    symbol='AAPL',
    stock_price_adjustment_setting=norgatedata.StockPriceAdjustmentType.TOTALRETURN,
    padding_setting=norgatedata.PaddingType.NONE,
    start_date='2020-01-01',
    timeseriesformat='pandas-dataframe'
)
# Returns: DataFrame with Date, Open, High, Low, Close, Volume, Turnover
```

#### B. Delisted Symbol Universe
```python
active = norgatedata.database_symbols('US Equities')
delisted = norgatedata.database_symbols('US Equities Delisted')
all_symbols = list(set(active + delisted))
```

#### C. Index Constituent Time Series
```python
# Check if symbol was in Russell 3000 on any given date
idx = norgatedata.index_constituent_timeseries(
    'AAPL', 'Russell 3000',
    timeseriesformat='pandas-dataframe'
)
# Returns: DataFrame with Date, Index Constituent (True/False)
```

#### D. Fundamentals
```python
mktcap, date = norgatedata.fundamental('AAPL', 'mktcap')
```

### Arconian Usage
- **Backtesting only** — not used for live signals (live signals use Schwab API)
- **Point-in-time universe construction** for historical signal validation
- **Delay score computation on historical data** (using delisted + active symbols)
- **Survivorship-bias-free backtest** per Section 2.4 of whitepaper

### Critical Constraint
NDU (Norgate Data Updater) is a Windows desktop application that must be running for the Python package to work. It syncs data daily. If the Arconian system runs on Linux, Norgate must be accessed via a Windows VM or a separate Windows machine with shared storage. The data adapter should abstract this behind an interface so the live system (Schwab-based) and historical system (Norgate-based) use the same `PriceDataProvider` contract.

### Failure Modes & Fallbacks
| Failure | Detection | Fallback |
|---|---|---|
| NDU not running | `norgatedata` raises connection error | This only affects backtest/historical analysis, not live trading. Alert operator, retry after starting NDU. |
| Subscription lapsed | Database becomes inaccessible | Renew subscription. No backtest or historical analysis possible without it. |
| Symbol not found | `norgatedata.price_timeseries()` returns empty | Check delisted universe; symbol may have changed ticker. Use Norgate's symbol change tracking. |

---

## 1.7 Earnings Calendar (Supplementary Source)

**Primary:** yfinance `earnings_dates` (see 1.5)
**Secondary/Validation:** Earnings Whispers or Alpha Vantage

### Earnings Whispers
- **URL:** `https://www.earningswhispers.com/stocks/{symbol}`
- **Access:** Web scrape (no official API)
- **Data:** Next earnings date, EPS estimate, revenue estimate, whisper number
- **Rate limit:** Be polite — 1 request/second max
- **Arconian usage:** Cross-validate yfinance earnings dates; resolve discrepancies by taking the more conservative (earlier) date for exclusion zone purposes

### Alpha Vantage (Free Tier)
- **Endpoint:** `GET https://www.alphavantage.co/query?function=EARNINGS_CALENDAR&horizon=3month&apikey={key}`
- **Rate limit:** 25 requests/day (free), 500 req/min (premium)
- **Response:** CSV with `symbol`, `reportDate`, `fiscalDateEnding`, `estimate`
- **Arconian usage:** Bulk download of upcoming earnings dates for entire universe at once (1 API call). Update daily.

---

## 1.8 Macro Calendar

**Purpose:** Identify FOMC, CPI, NFP, and other macro event dates that may cause market-wide volatility unrelated to individual stock signals.

### Implementation
A static Python module (`macro_calendar.py`) that:
1. Loads FOMC meeting dates from the Federal Reserve website (published annually)
2. Loads BLS release schedule for CPI, NFP (published annually)
3. Exposes a function: `is_macro_event_day(date) -> Optional[str]` returning event name or None
4. Used by the risk engine to optionally tighten position sizing on macro event days

No external API required — these dates are published well in advance and can be hardcoded annually with a January refresh.

---

# Document 2: Configuration Parameter Registry

Every tunable parameter, organized by module. Each entry specifies the default value, valid range, update mechanism, and owning module.

---

## 2.1 Universe Parameters

| Parameter | Default | Valid Range | Update Mechanism | Owner |
|---|---|---|---|---|
| `universe.market_cap_min_mm` | 500 | 200–1000 | Static (manual) | Universe Manager |
| `universe.market_cap_max_mm` | 2000 | 1000–5000 | Static (manual) | Universe Manager |
| `universe.min_daily_dollar_vol_mm` | 5.0 | 1.0–20.0 | Static (manual) | Universe Manager |
| `universe.min_midday_dollar_vol_mm` | 1.5 | 0.5–5.0 | Static (manual) | Universe Manager |
| `universe.max_spread_bps` | 40 | 20–100 | Static (manual) | Universe Manager |
| `universe.min_options_oi` | 1500 | 500–5000 | Static (manual) | Universe Manager |
| `universe.min_options_strike_count` | 4 | 2–10 | Static (manual) | Universe Manager |
| `universe.min_trading_days` | 60 | 30–120 | Static (manual) | Universe Manager |
| `universe.observation_mode_threshold_days` | 120 | 60–252 | Static (manual) | Universe Manager |
| `universe.delay_score_primary_threshold` | 0.3 | 0.1–0.5 | Static (manual) | Universe Manager |
| `universe.refresh_day` | "first_trading_day_of_month" | — | Static | Universe Manager |
| `universe.suspension_consecutive_fail_days` | 5 | 1–20 | Static (manual) | Universe Manager |

## 2.2 Signal Parameters

| Parameter | Default | Valid Range | Update Mechanism | Owner |
|---|---|---|---|---|
| `signal.weight_volume` | 0.30 | 0.05–0.50 | IC-recalibrated (60d) | Signal Engine |
| `signal.weight_return` | 0.25 | 0.05–0.50 | IC-recalibrated (60d) | Signal Engine |
| `signal.weight_options` | 0.15 | 0.05–0.25 | IC-recalibrated (30d first 6mo, then 60d) | Signal Engine |
| `signal.weight_sector_rs` | 0.10 | 0.05–0.30 | IC-recalibrated (60d) | Signal Engine |
| `signal.weight_delay` | 0.10 | 0.05–0.30 | IC-recalibrated (60d) | Signal Engine |
| `signal.weight_floor` | 0.05 | 0.01–0.10 | Static (manual) | Signal Engine |
| `signal.retail_penalty_weight` | 0.20 | 0.0–0.40 | IC-recalibrated (60d) | Signal Engine |
| `signal.volume_windows` | [20, 60, 120] | — | Static | Signal Engine |
| `signal.return_window` | 60 | 20–120 | Static (manual) | Signal Engine |
| `signal.sector_rs_window` | 60 | 20–120 | Static (manual) | Signal Engine |
| `signal.options_min_daily_volume` | 200 | 50–500 | Static (manual) | Signal Engine |
| `signal.options_min_trade_sizes` | 3 | 2–10 | Static (manual) | Signal Engine |
| `signal.iv_range_min_pct_points` | 5 | 2–10 | Static (manual) | Signal Engine |
| `signal.ic_recalibration_period_days` | 60 | 30–120 | Static (manual) | Signal Engine |
| `signal.ic_decay_threshold` | 0.02 | 0.01–0.05 | Static (manual) | Signal Engine |
| `signal.ic_consecutive_periods_for_flag` | 2 | 1–4 | Static (manual) | Signal Engine |
| `signal.earnings_exclusion_days_before` | 1 | 0–3 | Static (manual) | Signal Engine |
| `signal.earnings_exclusion_days_after` | 1 | 0–3 | Static (manual) | Signal Engine |
| `signal.corp_action_secondary_exclusion_days` | 5 | 3–10 | Static (manual) | Signal Engine |
| `signal.corp_action_reverse_split_exclusion_days` | 3 | 2–5 | Static (manual) | Signal Engine |
| `signal.corp_action_merger_exclusion` | "duration_of_deal" | — | Static | Signal Engine |
| `signal.corp_action_spac_exclusion_days` | 10 | 5–20 | Static (manual) | Signal Engine |
| `signal.corp_action_ticker_change_exclusion_days` | 5 | 3–10 | Static (manual) | Signal Engine |

## 2.3 Risk Parameters

| Parameter | Default | Valid Range | Update Mechanism | Owner |
|---|---|---|---|---|
| `risk.base_risk_per_trade` | 0.01 | 0.005–0.02 | Static (manual) | Risk Engine |
| `risk.phase1_risk_per_trade` | 0.005 | 0.0025–0.01 | Static (manual) | Risk Engine |
| `risk.atr_stop_multiplier` | 1.5 | 1.0–3.0 | Static (manual) | Risk Engine |
| `risk.catalyst_atr_premium` | 1.5 | 1.0–2.0 | Static (manual) | Risk Engine |
| `risk.max_position_pct` | 0.10 | 0.05–0.20 | Static (manual) | Risk Engine |
| `risk.max_concurrent_positions` | 5 | 3–8 | Static (manual) | Risk Engine |
| `risk.max_sector_positions` | 2 | 1–3 | Static (manual) | Risk Engine |
| `risk.max_sector_capital_pct` | 0.30 | 0.20–0.50 | Static (manual) | Risk Engine |
| `risk.max_correlated_cluster` | 3 | 2–5 | Static (manual) | Risk Engine |
| `risk.correlation_threshold` | 0.6 | 0.4–0.8 | Static (manual) | Risk Engine |
| `risk.correlation_lookback_days` | 60 | 30–120 | Static (manual) | Risk Engine |
| `risk.max_overnight_exposure_pct` | 0.60 | 0.30–0.80 | Static (manual) | Risk Engine |
| `risk.vix_elevated_threshold` | 25 | 20–35 | Static (manual) | Risk Engine |
| `risk.vix_crisis_threshold` | 35 | 30–45 | Static (manual) | Risk Engine |
| `risk.iwm_elevated_10d_return` | -0.05 | -0.10 to -0.02 | Static (manual) | Risk Engine |
| `risk.iwm_crisis_20d_return` | -0.10 | -0.15 to -0.05 | Static (manual) | Risk Engine |
| `risk.consecutive_loss_trigger` | 5 | 3–8 | Static (manual) | Risk Engine |
| `risk.drawdown_7pct_position_limit` | 3 | 2–4 | Static (manual) | Risk Engine |
| `risk.drawdown_7pct_recovery_days` | 15 | 10–30 | Static (manual) | Risk Engine |
| `risk.drawdown_12pct_halt` | true | — | Static | Risk Engine |
| `risk.single_day_loss_halt_pct` | 0.03 | 0.02–0.05 | Static (manual) | Risk Engine |
| `risk.single_day_loss_halt_days` | 2 | 1–5 | Static (manual) | Risk Engine |

## 2.4 Execution Parameters

| Parameter | Default | Valid Range | Update Mechanism | Owner |
|---|---|---|---|---|
| `execution.limit_buffer_bps` | 5 | 0–20 | Static (manual) | Execution Engine |
| `execution.fill_window_seconds` | 300 | 60–600 | Static (manual) | Execution Engine |
| `execution.partial_fill_min_pct` | 0.50 | 0.30–0.80 | Static (manual) | Execution Engine |
| `execution.partial_fill_accept_pct` | 0.80 | 0.60–0.95 | Static (manual) | Execution Engine |
| `execution.tp1_exit_pct` | 0.50 | 0.25–0.75 | Static (manual) | Execution Engine |
| `execution.tp1_r_multiple` | 2.0 | 1.5–3.0 | Static (manual) | Execution Engine |
| `execution.trailing_stop_atr_multiple` | 1.0 | 0.5–2.0 | Static (manual) | Execution Engine |
| `execution.time_stop_days` | 3 | 2–5 | Static (manual) | Execution Engine |
| `execution.scan_times_et` | ["09:35", "12:00", "15:30"] | — | Static (manual) | Scan Scheduler |
| `execution.pass1_top_n` | 40 | 20–80 | Static (manual) | Scan Scheduler |

## 2.5 Cost Model Parameters

| Parameter | Default | Valid Range | Update Mechanism | Owner |
|---|---|---|---|---|
| `cost.half_spread_bps` | 16 | 10–30 | Monitored (auto-log actuals) | Cost Model |
| `cost.slippage_bps` | 15 | 5–30 | Monitored (auto-log actuals) | Cost Model |
| `cost.adverse_selection_bps` | 20 | 10–40 | Monitored (auto-log actuals) | Cost Model |
| `cost.borrow_cost_bps_default` | 50 | 15–100 | Monitored (auto-log actuals) | Cost Model |
| `cost.working_roundtrip_long_bps` | 120 | 60–200 | Adjusted if actuals diverge >30% | Cost Model |
| `cost.working_roundtrip_short_bps` | 150 | 80–250 | Adjusted if actuals diverge >30% | Cost Model |
| `cost.exceedance_threshold_pct` | 0.30 | 0.20–0.50 | Static (manual) | Cost Model |
| `cost.exceedance_rolling_window` | 20 | 10–40 | Static (manual) | Cost Model |

## 2.6 Tax Parameters

| Parameter | Default | Valid Range | Update Mechanism | Owner |
|---|---|---|---|---|
| `tax.account_type` | "roth_ira" | "roth_ira", "taxable", "traditional_ira" | Static (manual) | Tax Module |
| `tax.effective_rate_taxable` | 0.50 | 0.30–0.55 | Static (manual) | Tax Module |
| `tax.effective_rate_roth` | 0.00 | 0.00 | Static | Tax Module |

## 2.7 Dead-Man's Switch Parameters

| Parameter | Default | Valid Range | Update Mechanism | Owner |
|---|---|---|---|---|
| `deadman.scan_timeout_minutes` | 60 | 30–120 | Static (manual) | System Monitor |
| `deadman.widen_stops_minutes` | 10 | 5–20 | Static (manual) | System Monitor |
| `deadman.force_close_minutes` | 30 | 15–60 | Static (manual) | System Monitor |
| `deadman.telegram_bot_token` | ENV_VAR | — | Static (manual) | System Monitor |
| `deadman.telegram_chat_id` | ENV_VAR | — | Static (manual) | System Monitor |

## 2.8 ML Parameters (Phase 2+)

| Parameter | Default | Valid Range | Update Mechanism | Owner |
|---|---|---|---|---|
| `ml.min_events_logistic` | 700 | 500–1500 | Static (manual) | ML Pipeline |
| `ml.min_events_lightgbm` | 8000 | 5000–15000 | Static (manual) | ML Pipeline |
| `ml.sharpe_improvement_threshold` | 0.05 | 0.03–0.10 | Static (manual) | ML Pipeline |
| `ml.purge_days` | 5 | 3–10 | Static (manual) | ML Pipeline |
| `ml.embargo_days` | 5 | 3–10 | Static (manual) | ML Pipeline |
| `ml.walk_forward_window_days` | 60 | 30–120 | Static (manual) | ML Pipeline |
| `ml.calibration_slope_tolerance` | 0.20 | 0.10–0.30 | Static (manual) | ML Pipeline |
| `ml.lgbm_max_depth` | 4 | 2–6 | Static (manual) | ML Pipeline |
| `ml.lgbm_num_leaves` | 15 | 8–31 | Static (manual) | ML Pipeline |
| `ml.lgbm_min_data_in_leaf` | 100 | 50–200 | Static (manual) | ML Pipeline |
| `ml.lgbm_learning_rate` | 0.01 | 0.005–0.05 | Static (manual) | ML Pipeline |

### Configuration File Format

```yaml
# arconian_config.yaml
# =====================
# Version: 1.0.0
# Last modified: 2026-04-06
# 
# Parameter changes are logged to parameter_history table
# with timestamp, old_value, new_value, reason.

universe:
  market_cap_min_mm: 500
  market_cap_max_mm: 2000
  min_daily_dollar_vol_mm: 5.0
  min_midday_dollar_vol_mm: 1.5
  max_spread_bps: 40
  min_options_oi: 1500
  min_options_strike_count: 4
  min_trading_days: 60
  observation_mode_threshold_days: 120
  delay_score_primary_threshold: 0.3
  refresh_day: "first_trading_day_of_month"
  suspension_consecutive_fail_days: 5

signal:
  weights:
    volume: 0.30
    return: 0.25
    options: 0.15
    sector_rs: 0.10
    delay: 0.10
  weight_floor: 0.05
  retail_penalty_weight: 0.20
  # ... (remaining signal params)

risk:
  base_risk_per_trade: 0.01
  # ... (remaining risk params)

# etc.
```

### Parameter History Table

```sql
CREATE TABLE parameter_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    parameter_path TEXT NOT NULL,    -- e.g., 'signal.weights.volume'
    old_value TEXT NOT NULL,
    new_value TEXT NOT NULL,
    reason TEXT NOT NULL,            -- e.g., 'IC recalibration period 3'
    triggered_by TEXT NOT NULL       -- 'ic_recalibration', 'manual', 'circuit_breaker'
);
```

At system startup, the config is loaded and the current parameter state is hashed. If the hash differs from the last recorded state, all changes are logged to `parameter_history`. IC-recalibrated weights are written both to the config file and to this table.

---

# Document 3: Universe State Machine & Null Propagation Rules

---

## 3.1 Universe States

Each ticker in the system exists in exactly one of five states at any given time.

```
                    meets all 6 filters,
                    history < 120d
  ┌──────────┐     ┌──────────────┐      history ≥ 120d     ┌────────┐
  │ CANDIDATE │────▶│  OBSERVATION  │─────────────────────────▶│ ACTIVE │
  └──────────┘     └──────────────┘                          └────────┘
       │                  │                                      │
       │                  │  fails filter                        │ fails filter
       │                  │  for ≥ N days                        │ for ≥ N days
       │                  ▼                                      ▼
       │           ┌─────────────┐                         ┌─────────────┐
       │           │  SUSPENDED   │                         │  SUSPENDED   │
       │           └─────────────┘                         └─────────────┘
       │                  │                                      │
       │                  │ fails filter                         │ fails filter
       │                  │ for ≥ 30 days                        │ for ≥ 30 days
       │                  ▼                                      ▼
       │           ┌─────────────┐                         ┌─────────────┐
       └──────────▶│   REMOVED    │◀────────────────────────│   REMOVED    │
                   └─────────────┘                         └─────────────┘
                          │
                          │ re-meets all filters
                          ▼
                    ┌──────────┐
                    │ CANDIDATE │  (re-enters cycle)
                    └──────────┘
```

### State Definitions

**CANDIDATE:** Ticker has been identified by the screener as potentially meeting universe criteria but has not yet been validated across a full monthly refresh cycle. Stocks enter as CANDIDATE during the monthly universe refresh. Transition to OBSERVATION or ACTIVE occurs within the same refresh cycle after all filters are evaluated.

**OBSERVATION:** Ticker meets all 6 universe filters (market cap, daily volume, midday volume, spread, options OI, trading history ≥ 60 days) but has fewer than 120 trading days of history. All data is collected and logged. Signals are computed but NOT acted upon. No trades generated.

**ACTIVE:** Ticker meets all 6 filters and has ≥ 120 trading days of history. Full signal scoring and trade generation is enabled.

**SUSPENDED:** Ticker was previously ACTIVE or OBSERVATION but has failed one or more universe filters for `suspension_consecutive_fail_days` (default: 5) consecutive trading days. Data collection continues (for existing signal_log entries awaiting outcome measurement). No new signals are generated. Positions still held are managed normally (stops, exits) but no new entries.

**REMOVED:** Ticker has been SUSPENDED for ≥ 30 consecutive calendar days, or has been manually removed via the blocklist, or has been delisted. No data collection. Historical data retained in signal_log.

### Transition Rules with Hysteresis

| Transition | Condition | Hysteresis |
|---|---|---|
| CANDIDATE → OBSERVATION | Passes all 6 filters; history < 120d | Checked during monthly refresh |
| CANDIDATE → ACTIVE | Passes all 6 filters; history ≥ 120d | Checked during monthly refresh |
| OBSERVATION → ACTIVE | History crosses 120d threshold | Checked daily (simple day count) |
| ACTIVE → SUSPENDED | Fails ANY filter for 5 consecutive trading days | Must fail the SAME filter for 5 days (not different filters on different days) |
| OBSERVATION → SUSPENDED | Fails ANY filter for 5 consecutive trading days | Same as above |
| SUSPENDED → ACTIVE | Passes ALL 6 filters for 3 consecutive trading days AND history ≥ 120d | 3-day confirmation prevents thrashing |
| SUSPENDED → OBSERVATION | Passes ALL 6 filters for 3 consecutive trading days AND history < 120d | Same |
| SUSPENDED → REMOVED | 30 consecutive calendar days in SUSPENDED | Automatic |
| REMOVED → CANDIDATE | Re-passes all filters during monthly refresh | Re-enters the full lifecycle |
| ANY → REMOVED | Manual blocklist entry or delisting detected | Immediate, no hysteresis |

### Key Design Decision: Which Filter Caused Suspension?

The `universe_state` table records the specific filter that triggered suspension:

```sql
CREATE TABLE universe_state (
    ticker TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('CANDIDATE','OBSERVATION','ACTIVE','SUSPENDED','REMOVED')),
    state_since DATETIME NOT NULL,
    history_days INTEGER,
    suspension_reason TEXT,           -- which filter failed, e.g., 'midday_vol < 1.5M'
    suspension_start DATE,
    consecutive_fail_days INTEGER DEFAULT 0,
    consecutive_pass_days INTEGER DEFAULT 0,  -- for re-entry hysteresis
    delay_score REAL,
    delay_score_as_of DATE,
    last_checked DATETIME,
    manual_block BOOLEAN DEFAULT FALSE,
    manual_block_reason TEXT
);
```

---

## 3.2 Daily Universe Check (Runs at 8:00 AM ET, Pre-Market)

```
For each ticker in state ACTIVE or OBSERVATION:
  1. Pull latest 20-day trailing data from Schwab API
  2. Check all 6 filters:
     a. Market cap (from yfinance .info, cached weekly)
     b. 20-day avg daily dollar volume
     c. 20-day avg midday dollar volume (from stored intraday data)
     d. 20-day avg bid-ask spread
     e. Options OI (from most recent options chain pull)
     f. Trading history day count
  3. If ALL pass:
     - Reset consecutive_fail_days = 0
     - Increment consecutive_pass_days (if SUSPENDED)
     - If SUSPENDED and consecutive_pass_days ≥ 3: transition to ACTIVE/OBSERVATION
  4. If ANY fail:
     - Increment consecutive_fail_days
     - Reset consecutive_pass_days = 0
     - If consecutive_fail_days ≥ 5: transition to SUSPENDED
     - Log which filter failed

For each ticker in state SUSPENDED:
  1. Check if 30 days have elapsed since suspension_start
     - If yes: transition to REMOVED
  2. Also run filter check (for possible re-entry)
```

---

## 3.3 NULL Propagation Rules

When a signal component cannot be computed (missing data, insufficient history, data source outage), the system must handle NULLs consistently.

### Signal-Level NULL Handling

| Signal | NULL Condition | Action |
|---|---|---|
| Volume Surprise | History < 20 days (shouldn't happen per universe filter, but defensive) | Set to 0.5 (neutral). Log `volume_pctile_*d = NULL, defaulted`. |
| Volume 120d window | History < 120 days (OBSERVATION mode) | Use only 20d and 60d windows. Composite of available windows only. Flag `history_status = 'short'`. |
| Return Magnitude | History < 60 days | Set to 0.5 (neutral). Flag. |
| Options Composite | No options data (OI < 1500, or < 4 strikes, or daily vol < 200) | Set to NULL. Redistribute options weight (0.15) proportionally across remaining signals. |
| Sector RS | Sector ETF data unavailable | Set to 0.5 (neutral). Log `sector_rs_data_unavailable`. |
| Delay Score | Stock in OBSERVATION mode with < 52 weeks history | Set to NULL. Redistribute delay weight (0.10) proportionally. Note: stocks in OBSERVATION mode don't generate trade signals anyway, but the score is still logged for future analysis. |
| Retail Attention (StockTwits) | API failure or rate limited | Set social_velocity to 0.0 (no penalty). Log `stocktwits_unavailable`. |
| Retail Attention (Reddit) | API failure or rate limited | Set reddit_mentions to 0 (no penalty). Log `reddit_unavailable`. |
| Retail Attention (Scanner flag) | No scanner data source available | Set scanner_flag to 0.0 (no penalty). Log. |

### Weight Redistribution Formula

When a signal with weight `w_null` is NULL:
```python
remaining_weights = {k: v for k, v in weights.items() if signal[k] is not None}
total_remaining = sum(remaining_weights.values())
adjusted_weights = {k: v / total_remaining for k, v in remaining_weights.items()}
```

This preserves the relative proportions of the available signals while ensuring the composite score remains in [0, 1].

### Composite Score with NULLs

```python
def compute_composite(signals: dict, weights: dict, retail_penalty: float, penalty_weight: float) -> float:
    available = {k: v for k, v in signals.items() if v is not None and k != 'retail_attention'}
    if not available:
        return None  # Cannot score — insufficient data
    
    # Redistribute weights
    active_weights = {k: weights[k] for k in available}
    total_w = sum(active_weights.values())
    norm_weights = {k: w / total_w for k, w in active_weights.items()}
    
    raw_composite = sum(norm_weights[k] * available[k] for k in available)
    
    # Apply retail penalty (retail_penalty defaults to 0.0 if unavailable)
    effective_penalty = retail_penalty if retail_penalty is not None else 0.0
    penalized_composite = raw_composite * (1 - penalty_weight * effective_penalty)
    
    return penalized_composite
```

---

## 3.4 GICS Sector → ETF Mapping Table

This is the complete, hardcoded lookup table. yfinance returns sector names that must be normalized to GICS before mapping to ETFs.

```python
GICS_SECTOR_ETF_MAP = {
    # GICS Sector Name           → SPDR Sector ETF
    "Information Technology":       "XLK",
    "Health Care":                  "XLV",
    "Financials":                   "XLF",
    "Consumer Discretionary":       "XLY",
    "Communication Services":       "XLC",
    "Industrials":                  "XLI",
    "Consumer Staples":             "XLP",
    "Energy":                       "XLE",
    "Utilities":                    "XLU",
    "Real Estate":                  "XLRE",
    "Materials":                    "XLB",
}

# yfinance sector names → GICS normalized names
# yfinance sometimes returns slightly different strings
YFINANCE_SECTOR_NORMALIZE = {
    "Technology":                   "Information Technology",
    "Healthcare":                   "Health Care",
    "Financial Services":           "Financials",
    "Consumer Cyclical":            "Consumer Discretionary",
    "Communication Services":       "Communication Services",  # same
    "Industrials":                  "Industrials",             # same
    "Consumer Defensive":           "Consumer Staples",
    "Energy":                       "Energy",                  # same
    "Utilities":                    "Utilities",               # same
    "Real Estate":                  "Real Estate",             # same
    "Basic Materials":              "Materials",
}

def get_sector_etf(yfinance_sector: str) -> Optional[str]:
    """Map a yfinance sector string to the corresponding SPDR sector ETF."""
    gics_sector = YFINANCE_SECTOR_NORMALIZE.get(yfinance_sector)
    if gics_sector is None:
        logger.warning(f"Unknown yfinance sector: {yfinance_sector}")
        return None
    return GICS_SECTOR_ETF_MAP.get(gics_sector)
```

### Sector ETF Data Requirements
- Daily close prices for all 11 sector ETFs must be available for the trailing 60 days (for Sector RS computation)
- Pull via yfinance `yf.download(['XLK','XLV','XLF','XLY','XLC','XLI','XLP','XLE','XLU','XLRE','XLB'], period='3mo')` — single batched call, cache locally, refresh daily at 4:15 PM ET
- If a stock's sector cannot be determined (yfinance returns None), set `sector_rs_percentile = 0.5` (neutral) and log

---

## 3.5 Schema Migration Strategy

Use **Alembic** (SQLAlchemy's migration tool) from day one.

### Setup
```
arconian/
├── alembic/
│   ├── env.py
│   ├── versions/
│   │   ├── 001_initial_schema.py
│   │   ├── 002_add_retail_attention.py
│   │   └── ...
│   └── alembic.ini
├── models.py          # SQLAlchemy ORM models for signal_log, universe_state, etc.
└── db.py              # Database connection, WAL mode configuration
```

### Rules
1. **Every schema change goes through Alembic** — no manual `ALTER TABLE`
2. Migrations are versioned and timestamped
3. Both upgrade and downgrade paths must be defined
4. Run `alembic upgrade head` on startup to ensure schema is current
5. The `signal_log` table is append-only in production — migrations should only ADD columns, never remove or rename (old columns become deprecated, not deleted)
6. Test migrations on a copy of the production database before deploying

### Initial Migration
```python
# alembic/versions/001_initial_schema.py
def upgrade():
    # Create all tables from Section 7.1 of whitepaper
    op.create_table('signal_log', ...)
    op.create_table('outcome_prices', ...)
    op.create_table('universe_state', ...)
    op.create_table('parameter_history', ...)
    op.create_table('ic_history', ...)
    op.create_table('overrides', ...)  # manual blocklist, shared with Zinniinae

def downgrade():
    op.drop_table('overrides')
    op.drop_table('ic_history')
    op.drop_table('parameter_history')
    op.drop_table('universe_state')
    op.drop_table('outcome_prices')
    op.drop_table('signal_log')
```

---

## 3.6 Additional Implementation Notes

### Database Initialization
```python
import sqlite3

def init_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")  # 5s wait on lock contention
    conn.execute("PRAGMA synchronous=NORMAL;")  # WAL-safe, better performance
    return conn
```

### Trading Calendar
Use the `exchange_calendars` package (or `trading_calendars`) to determine:
- Is today a trading day?
- What are the market hours?
- What was the previous trading day? (needed for "prior day VWAP" reference)

```python
import exchange_calendars as xcals
nyse = xcals.get_calendar('XNYS')
# nyse.is_session(date) → bool
# nyse.previous_session(date) → Timestamp
```

### Scan Scheduler
Use `APScheduler` (Advanced Python Scheduler) for the three daily scans:
```python
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler(timezone='US/Eastern')
scheduler.add_job(run_scan, 'cron', hour=9, minute=35, args=['open'])
scheduler.add_job(run_scan, 'cron', hour=12, minute=0, args=['midday'])
scheduler.add_job(run_scan, 'cron', hour=15, minute=30, args=['preclose'])
scheduler.add_job(run_daily_maintenance, 'cron', hour=16, minute=15)  # outcome_prices, VWAP, sector ETF data
scheduler.add_job(run_universe_check, 'cron', hour=8, minute=0)  # pre-market universe state check
scheduler.start()
```

### Overrides Table (Shared with Zinniinae)
```sql
CREATE TABLE overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    override_type TEXT NOT NULL CHECK(override_type IN ('block', 'force_review', 'corp_action_manual')),
    reason TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT DEFAULT 'manual',   -- 'manual' or 'auto'
    expires_at DATETIME,                -- NULL = permanent until removed
    active BOOLEAN DEFAULT TRUE
);
```

---

# Summary: Architecture Handoff Checklist

Before Claude Code starts scaffolding modules, confirm:

- [ ] Schwab Developer Portal app is approved and credentials are available
- [ ] `schwab-py` installed and token.json generated via initial browser auth
- [ ] SEC EDGAR User-Agent string decided and CIK mapping file cached
- [ ] StockTwits approach decided: unauthenticated (200/hr) or OAuth app registered (400/hr)
- [ ] Reddit OAuth script-type app registered at reddit.com/prefs/apps
- [ ] Norgate Data subscription active (Platinum tier) and NDU running on Windows
- [ ] yfinance version pinned in requirements.txt
- [ ] Telegram bot created for alerting (token + chat_id)
- [ ] SQLite database path decided; WAL mode will be configured on first init
- [ ] `arconian_config.yaml` template created with all defaults from Document 2
- [ ] Alembic initialized with initial migration matching signal_log schema from whitepaper Section 7.1
- [ ] `exchange_calendars` or `trading_calendars` package selected and installed
- [ ] GICS → ETF mapping table and yfinance normalization map reviewed for completeness

### Recommended Module Structure for Claude Code

```
arconian/
├── config/
│   ├── arconian_config.yaml
│   └── config_loader.py
├── data/
│   ├── schwab_adapter.py        # All Schwab API calls
│   ├── edgar_adapter.py         # SEC filing queries
│   ├── social_adapter.py        # StockTwits + Reddit
│   ├── yfinance_adapter.py      # yfinance wrapper
│   ├── norgate_adapter.py       # Norgate historical data
│   ├── earnings_calendar.py     # Multi-source earnings dates
│   └── macro_calendar.py        # FOMC/CPI/NFP dates
├── universe/
│   ├── universe_manager.py      # State machine, filter checks
│   ├── delay_score.py           # Hou-Moskowitz computation
│   └── universe_state.py        # SQLAlchemy model
├── signals/
│   ├── signal_engine.py         # Composite scoring orchestrator
│   ├── volume_signal.py         # Percentile rank, multi-window
│   ├── return_signal.py
│   ├── options_signal.py        # With all safeguards
│   ├── sector_rs_signal.py
│   ├── retail_attention.py      # Penalty computation
│   └── directional_confirmation.py
├── risk/
│   ├── risk_engine.py           # Position sizer, regime detector
│   ├── portfolio_constraints.py # Sector, correlation, exposure
│   ├── cvar.py                  # CVaR + stress scenarios
│   └── circuit_breakers.py
├── execution/
│   ├── scan_scheduler.py        # APScheduler setup
│   ├── order_manager.py         # Phase 2+ order placement
│   ├── partial_fill_manager.py
│   └── deadman_switch.py
├── journal/
│   ├── signal_log.py            # SQLAlchemy model + write methods
│   ├── outcome_collector.py     # 4:15 PM daily task
│   ├── ic_tracker.py            # IC computation + history
│   ├── cost_validator.py        # Intended vs actual fills
│   └── parameter_tracker.py
├── models.py                    # All SQLAlchemy ORM models
├── db.py                        # DB connection, WAL config
├── main.py                      # Entry point, scheduler startup
├── alembic/
│   ├── env.py
│   └── versions/
└── tests/
    ├── test_signal_engine.py
    ├── test_universe_manager.py
    ├── test_risk_engine.py
    └── fixtures/                 # Mock API responses
```

This structure maps 1:1 to the whitepaper's system topology (Section 6.1) and keeps each concern isolated behind a clean interface.
