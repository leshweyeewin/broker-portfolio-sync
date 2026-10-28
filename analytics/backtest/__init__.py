"""Rule-based backtest package (Moomoo Day 3: turn an idea into a system).

Pure and read-only: it consumes bars you pass in, never fetches data, never
touches a broker.  Stage 1 is stock-level; option-level pricing plugs in later
behind the same ``RuleSpec`` / ``BacktestResult`` model.
"""
