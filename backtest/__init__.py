"""Backtesting package for crypto-scalp-bot.

Implements Filtered Replay backtesting: reconstructs the dynamic watchlist
from historical 24h price-change data and evaluates signals only on symbols
that would have been selected at each point in time.
"""
