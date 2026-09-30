# Task: Seed the Arconian Universe

## Context

Arconian is in Phase 0 — the signal engine, universe manager, risk engine, and all 8 build steps are complete. 316 tests pass, `python main.py --dry-run` runs clean. But every scan returns 0 candidates because the `universe_state` table in `arconian.db` is empty. We need to populate it.

## What to build

Create a script `scripts/seed_universe.py` that:

1. **Generates a broad candidate list (~500–800 small-cap tickers)** in the $500M–$2B market cap range. Use `yfinance` to screen for these. The approach:
   - Download the full ticker list. yfinance doesn't have a built-in screener, so use one of these approaches (try in order, use whichever works):
     - **Option A:** Use `yf.Screener` / `yf.screen` if available in the installed yfinance version
     - **Option B:** Fetch Russell 2000 (IWM) or S&P 600 SmallCap (IJR) holdings — try `yf.Ticker('IWM').get_funds_data()` or similar methods to get constituent lists
     - **Option C:** If neither A nor B works, use a static list approach: download a publicly available list of US equities (e.g., from the SEC's `company_tickers.json` at `https://www.sec.gov/files/company_tickers.json`), then batch-check market caps via `yf.download()` or `yf.Ticker().info` to filter to the $500M–$2B range
   - Whatever approach you use, the goal is a list of 500–800 US equity tickers in the $500M–$2B market cap band. Don't overthink it — this is a one-time seed; the monthly refresh will refine it.

2. **Feed the candidates to `UniverseManager.run_monthly_refresh()`**. This method already exists and handles all 6 filter checks (market cap, daily dollar volume, midday volume, spread, options OI, trading history). It writes passing tickers to the `universe_state` table as ACTIVE or OBSERVATION.

3. **Report results** — print a summary showing: how many candidates were screened, how many passed (broken down by ACTIVE vs OBSERVATION), how many failed (and the most common failure reason), and list the first 20 ACTIVE tickers.

## Key interfaces you'll use

```python
# Already in the codebase — import and use as-is:
from config.config_loader import ConfigLoader
from data.yfinance_adapter import YFinanceAdapter
from data.schwab_adapter import SchwabAdapter
from universe.universe_manager import UniverseManager
from db import init_db

# Init sequence:
config = ConfigLoader("config/arconian_config.yaml")
init_db()
yf_adapter = YFinanceAdapter()

# Schwab is optional for the monthly refresh — spread/options/midday checks
# return None (inconclusive) when schwab_adapter=None, which means those
# filters are treated as "pass" (not "fail"). This is fine for initial seeding.
# The daily check will catch tickers that fail those filters once Schwab is live.
schwab = None  # or SchwabAdapter.from_token_file() if token is valid

universe_mgr = UniverseManager(config=config, yf_adapter=yf_adapter, schwab_adapter=schwab)

# The main call:
outcomes = universe_mgr.run_monthly_refresh(candidate_tickers)
# Returns dict[str, str] mapping ticker → outcome:
#   'added_active', 'added_observation', 'skipped_failed_filters',
#   'skipped_already_tracked', 'skipped_blocked', 'skipped_insufficient_history'
```

## Important notes

- **Run from the project root** (`C:\Users\<user>\Documents\arconian`) so imports resolve correctly. Add `sys.path.insert(0, ...)` if needed.
- **Load .env** before importing adapters: `from dotenv import load_dotenv; load_dotenv()` — this is how `main.py` does it.
- **yfinance rate limiting:** yfinance can throttle aggressively if you hammer it. When bulk-checking market caps, use `yf.download()` for batch price data, and add ~0.5s delays between `.info` calls if needed. The `YFinanceAdapter` already has some caching built in.
- **The universe filters in `run_monthly_refresh` do the heavy lifting.** Your script only needs to produce the broad candidate list. Don't re-implement the filters — just pass tickers in and let the manager sort them out.
- **Without Schwab, the initial seed will be permissive** — spread, options OI, options strike count, and midday volume checks will return None (inconclusive), which `FilterCheckResult.all_pass` treats as passing. This means more tickers will enter as ACTIVE/OBSERVATION than will survive the daily check once Schwab is connected. That's fine — the daily check handles pruning.
- **Database is at `arconian.db`** in the project root, already initialized with the schema via Alembic.
- **Don't batch too large** — yfinance `.info` calls are the bottleneck (they're individual HTTP requests). If you're checking 3000+ tickers' market caps, consider chunking with progress output so we can see it's working.

## Verification

After running the script, verify by running:
```python
from db import session_scope
from models import UniverseState
with session_scope() as s:
    counts = {}
    for r in s.query(UniverseState).all():
        counts[r.state] = counts.get(r.state, 0) + 1
    print(counts)
```

We should see something like `{'ACTIVE': 150-300, 'OBSERVATION': 10-50}` depending on how many pass the yfinance-only filters.

Also verify that `python main.py --dry-run` now produces scored candidates instead of "no tickers in ACTIVE/OBSERVATION state".
