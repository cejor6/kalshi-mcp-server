"""MCP resources.

Resources expose Kalshi state as URI-addressable data the model can read
without spending a tool call. Planned URIs:

    kalshi://environment              — prod/demo, tier, current bucket levels
    kalshi://balance                  — cash + portfolio value
    kalshi://positions                — open positions
    kalshi://orders/open              — resting orders
    kalshi://markets/{ticker}         — single market snapshot
    kalshi://markets/{ticker}/orderbook  — live (WS-backed when streaming)
    kalshi://events/{ticker}          — event with nested markets
    kalshi://series/{ticker}          — series metadata

None registered yet — coming in subsequent commits.
"""

from __future__ import annotations
