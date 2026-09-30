"""
Retail Attention penalty computation.

High retail attention → compressed information delay window → reduced
composite score. Names already widely covered by scanner tools and social
platforms have less residual delay to exploit.

Formula:
  social_velocity = percentile_rank(mentions_24h, trailing_60d_mentions)
  scanner_flag    = 1.0 if trending on major scanner platform, else 0.0
  si_crowd        = 1.0 if short_interest > 20% of float, else 0.0

  retail_attention = 0.5 × social_velocity
                   + 0.3 × scanner_flag
                   + 0.2 × si_crowd

Applied as a penalty in the composite score:
  penalized_composite = raw_composite × (1 − penalty_weight × retail_attention)

Where penalty_weight defaults to 0.20 (20% maximum reduction at full attention).

Graceful degradation: if any component's data source is unavailable, that
component defaults to 0.0 (no penalty applied), preserving signal integrity
without falsely penalising names with missing data.

Whitepaper reference: Section 3.2.6 (Retail Attention Penalty)
Supplements reference: Doc 1, Sections 1.3–1.4 (social data contracts)
"""

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_SI_CROWD_THRESHOLD = 0.20   # 20% short interest of float
_WEIGHTS = {
    "social_velocity": 0.5,
    "scanner_flag": 0.3,
    "si_crowd": 0.2,
}


def _pctile_rank(value: float, history: np.ndarray) -> float:
    """Fraction of `history` values strictly below `value`. Range [0, 1]."""
    if len(history) == 0:
        return 0.0
    return float(np.mean(history < value))


@dataclass
class RetailAttentionResult:
    """
    Breakdown of the retail attention penalty score.

    Each component is in [0, 1]. Missing data → 0.0 (no penalty for that component).
    The composite `score` is in [0, 1] with higher values indicating more retail
    attention and thus a stronger penalty applied downstream.
    """

    score: float                          # composite [0, 1]
    social_velocity: float                # mentions percentile rank
    scanner_flag: float                   # 0.0 or 1.0
    si_crowd: float                       # 0.0 or 1.0
    social_unavailable: bool = False      # True if StockTwits/Reddit API failed
    scanner_unavailable: bool = False     # True if no scanner data source available


def compute_retail_attention(
    mentions_24h: Optional[float],
    history_mentions: Optional[np.ndarray],
    scanner_flag: Optional[bool],
    short_interest_pct: Optional[float],
    si_threshold: float = _SI_CROWD_THRESHOLD,
) -> RetailAttentionResult:
    """
    Compute the retail attention penalty score [0, 1].

    Args:
        mentions_24h: Raw mention count across StockTwits + Reddit in the
            trailing 24 hours. None if social API is unavailable.
        history_mentions: Trailing 60d array of daily mention counts for this
            stock (oldest first). Used to compute social velocity percentile.
            None if history is unavailable; percentile defaults to 0.0.
        scanner_flag: True if the stock appeared on major retail scanner
            platforms (Trade Ideas, Unusual Whales, etc.) in the last 24h.
            None if scanner data is unavailable → defaults to 0.0 (no penalty).
        short_interest_pct: Short interest as a fraction of float (e.g., 0.25
            for 25%). None if unavailable → SI component defaults to 0.0.
        si_threshold: Short interest fraction above which crowding is flagged
            (default 0.20, i.e., 20% of float).

    Returns:
        RetailAttentionResult with composite score and component breakdown.
    """
    # ---- Social velocity ----
    social_unavailable = False
    if mentions_24h is None:
        social_vel = 0.0
        social_unavailable = True
        logger.debug("retail_attention: social data unavailable — social_velocity=0.0")
    elif history_mentions is not None and len(history_mentions) >= 5:
        hist = np.asarray(history_mentions, dtype=float)
        social_vel = _pctile_rank(float(mentions_24h), hist)
    else:
        # Mentions present but no history for comparison → treat as low (0.0 no penalty)
        social_vel = 0.0
        logger.debug("retail_attention: no mention history — social_velocity=0.0")

    # ---- Scanner flag ----
    scanner_unavailable = False
    if scanner_flag is None:
        sc_flag = 0.0
        scanner_unavailable = True
        logger.debug("retail_attention: scanner data unavailable — scanner_flag=0.0")
    else:
        sc_flag = 1.0 if scanner_flag else 0.0

    # ---- Short interest crowding ----
    if short_interest_pct is None:
        si = 0.0
        logger.debug("retail_attention: SI data unavailable — si_crowd=0.0")
    else:
        si = 1.0 if short_interest_pct >= si_threshold else 0.0

    # ---- Composite ----
    score = (
        _WEIGHTS["social_velocity"] * social_vel
        + _WEIGHTS["scanner_flag"] * sc_flag
        + _WEIGHTS["si_crowd"] * si
    )
    score = float(np.clip(score, 0.0, 1.0))

    logger.debug(
        "retail_attention: social=%.3f scanner=%.1f si=%.1f score=%.3f",
        social_vel, sc_flag, si, score,
    )

    return RetailAttentionResult(
        score=score,
        social_velocity=social_vel,
        scanner_flag=sc_flag,
        si_crowd=si,
        social_unavailable=social_unavailable,
        scanner_unavailable=scanner_unavailable,
    )
