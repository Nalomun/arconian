# Arconian

Arconian is a rule-based system I designed and built in 2026 to trade short-term drift in U.S. small-cap stocks with slow information diffusion, using a five-signal composite score and a 1–3 day holding period. A backtest suggested a weak but real signal, but per-day cross-sectional tests showed the composite does not rank stocks, either against raw returns or, in a pre-registered test, against returns signed by the direction of the day's move.

This repository is a public snapshot of private work. Arconian is the third iteration of Zinnia, a private project I started in April 2025.

**Status: closed. Null result.** The daemon hasn't run since June 2026, and no live orders were ever placed.

## Start here

**[docs/ARCONIAN_WRITEUP.md](docs/ARCONIAN_WRITEUP.md)** ([PDF](docs/ARCONIAN_WRITEUP.pdf)) covers the thesis, what was built, what broke, the two decisive tests, and what I'd do differently.

For the full forensic record (chronology, every number with its provenance, a methodology audit), see [PROJECT_HISTORY_ARCONIAN.md](PROJECT_HISTORY_ARCONIAN.md).

I built the system with AI-assisted development from a whitepaper I designed: I worked with Claude on the design documents and used Claude Code to generate the codebase and carry out each later piece of work from a written brief, while the thesis, the validation gates, the research questions and the calls about what the results meant were mine.

## Repository layout

| Path | Contents |
|---|---|
| `docs/` | The writeup (Markdown and PDF) and its figures |
| `docs/design/` | The original whitepaper (`arconian-v3.md`) and supplementary specifications |
| `docs/briefs/` | The written briefs each piece of AI-assisted work was carried out from |
| `docs/history/` | `DIAGNOSIS.md` (June 2026 audit, 36 findings) and `FIXLOG.md` (remediation log) |
| `PROJECT_HISTORY_ARCONIAN.md` | Forensic project history |
| `analysis/` | September 2026 research: per-day IC check, signed-return test and its pre-registration, per-day aggregate series |
| `analysis_2026-04-21.md` and the three other root-level `.md` reports | The April 2026 backtest analyses, as generated |
| `signals/`, `universe/`, `risk/`, `execution/`, `journal/`, `data/`, `config/` | The trading system: signal engine, universe manager, risk engine, paper-trade state machine, outcome labeling and IC tracking, data adapters, configuration |
| `scripts/` | Backtest and April analysis scripts, operational utilities |
| `scripts/research/` | The September research scripts cited in the writeup's Appendix A |
| `tests/` | 491 unit and integration tests |
| `main.py` | Daemon entry point (scheduled scans, paper trading) |

## Running the tests

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The suite needs no credentials, network access or market data: every external dependency is mocked. Expected result: 491 passed.

The research scripts need `pip install -e ".[research]"` plus the licensed inputs described below. Running the daemon itself needs Schwab API credentials in `.env` (see `.env.example`); it is not maintained.

## What the tests do and don't show

The 491 tests check that the code does what the design says. They cover the signals, the composite score, the risk arithmetic, the paper-trade state machine and the scheduler, all on inputs I constructed. Every outside dependency is mocked, and the suite passes with networking disabled. So the tests say nothing about whether the numbers are right on real market data, or whether the adapters handle what the vendors actually return.

## Data notice

**No vendor data is included in this repository.** The project used:

- **Norgate Data** (trial subscription): historical prices for the backtest. The backtest CSV (`backtest_2024-01-01_2026-04-21.csv`, one row per stock-day) was built from this licensed data and is not distributed.
- **CRSP via WRDS**: daily returns for the signed-return test. The pulled data is licensed and not distributed; `scripts/research/crsp_pull_day_t_returns.py` shows exactly what was queried.
- **Yahoo Finance via `yfinance`**: the trading calendar, sector ETFs, sector labels and live quotes. No cached responses are included.
- **SEC EDGAR** (public) and the **Schwab API** (live quotes): used at runtime only.

What is included are derived aggregates: per-day information-coefficient series, decile spreads and summary tables. None of them contains per-stock values.

Commit hashes cited in the writeup and in the project history refer to my private development repository; this repository is a snapshot of it. The pre-registration of the signed-return test was committed there on its own as commit `daf7354`, before any result on the matched sample was computed (the raw-return results of the first test had already been seen). The published file is byte-identical: its git blob hash is `a1812569fe85b9b76bc0209eac3478d6581ac004`, which you can check with `git hash-object analysis/signed_test_preregistration.md`.

Nothing here is investment advice.

## License

MIT (see [LICENSE](LICENSE)).
