"""
Directional confirmation layer.

Combines three signals to determine whether a high-composite-score event
should be approached as bullish, bearish, or ambiguous (no trade):

  Bullish: 1d_return > 0
           AND current_price > prior_day_closing_VWAP
           AND (if options available: call_vol > put_vol)

  Bearish: 1d_return < 0
           AND current_price < prior_day_closing_VWAP
           AND (if options available: put_vol > call_vol)
           AND borrow_available is not False

  Ambiguous: conflicting signals → no trade generated

The VWAP reference is the PRIOR DAY's closing VWAP, not the current
intraday VWAP. This eliminates scan-timing dependency (intraday VWAP
changes throughout the day; the prior-day closing VWAP is a fixed reference
at any scan time).

The ambiguous case is treated as a hard pass — no forced directional
interpretation. Roughly 30–50% of high-composite events are expected to be
ambiguous (whitepaper Section 3.4).

Whitepaper reference: Section 3.4 (Directional Confirmation Layer)
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Public constants for callers
BULLISH = "bullish"
BEARISH = "bearish"
AMBIGUOUS = "ambiguous"


def compute_directional_confirmation(
    return_1d: float,
    current_price: float,
    prior_day_vwap: Optional[float],
    call_vol: Optional[float] = None,
    put_vol: Optional[float] = None,
    borrow_available: Optional[bool] = None,
) -> str:
    """
    Classify the directional bias of a signal candidate.

    Args:
        return_1d: Today's 1-day return as a fraction (e.g., 0.03 for +3%).
        current_price: Current (or last-trade) price of the stock.
        prior_day_vwap: The previous trading day's closing volume-weighted
            average price. If None, the VWAP condition is treated as met
            (i.e., VWAP is not used as a blocker when data is unavailable).
        call_vol: Total call option contracts traded today. Optional — if
            neither call_vol nor put_vol is provided, the options directional
            filter is skipped (no penalty for missing options data).
        put_vol: Total put option contracts traded today. Optional.
        borrow_available: Whether borrow is available for the stock (for
            bearish signals). None means unknown — bearish is NOT blocked
            but a None borrow is noted in the signal_log for monitoring.

    Returns:
        'bullish', 'bearish', or 'ambiguous'.
    """
    # ---- VWAP comparison (skip if data unavailable) ----
    if prior_day_vwap is not None and prior_day_vwap > 0:
        price_above_vwap = current_price > prior_day_vwap
    else:
        # No VWAP data → treat this condition as inconclusive (not blocking)
        price_above_vwap = None
        logger.debug("directional: prior_day_vwap unavailable — skipping VWAP condition")

    # ---- Options directional bias (optional) ----
    options_bullish = options_bearish = None
    if call_vol is not None and put_vol is not None and (call_vol + put_vol) > 0:
        options_bullish = call_vol > put_vol
        options_bearish = put_vol > call_vol
    # If options data absent, we skip the options condition for both sides

    # ---- Bullish check ----
    is_bullish = (
        return_1d > 0
        and (price_above_vwap is True or price_above_vwap is None)
        and (options_bullish is True or options_bullish is None)
    )

    # ---- Bearish check ----
    # borrow_available=False is a hard block; None is allowed (uncertain borrow)
    borrow_ok = borrow_available is not False
    is_bearish = (
        return_1d < 0
        and (price_above_vwap is False or price_above_vwap is None)
        and (options_bearish is True or options_bearish is None)
        and borrow_ok
    )

    # ---- Classification ----
    if is_bullish and not is_bearish:
        result = BULLISH
    elif is_bearish and not is_bullish:
        result = BEARISH
    else:
        result = AMBIGUOUS

    logger.debug(
        "directional: return=%.4f vwap_above=%s options_bull=%s options_bear=%s "
        "borrow_ok=%s → %s",
        return_1d, price_above_vwap, options_bullish, options_bearish, borrow_ok, result,
    )
    return result
