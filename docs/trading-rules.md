# Trading rules (risk checklist)

Derived from the Aug–Oct 2026 account review. Two months: **-$919 (-40.9%)**,
and **~90% of that loss came from one trade** — a short put on SNDQ (a 2x
inverse single-stock ETF). The strategy wasn't the problem; position sizing and
instrument selection were. These rules exist so that one trade can never again
be bigger than every winning trade combined.

Not advice — a personal discipline checklist for this account.

## What the review found

- Win rate was ~50/50. That is fine **only** if winners ≈ losers. They weren't:
  winners totalled ~+$232, losers ~-$1,159, dominated by one uncapped trade.
- The "winning" spreads (AMZN, PLTR) were **5-point-wide** → ~$400 max loss each
  = **~30% of the account per trade**. They won by luck of expiry, not by sizing.
- MRVL was the same 5-wide structure that *didn't* expire OTM: -$263 ≈ full
  width = ~20% of the account on one trade.
- Three of five losers involved leveraged/inverse single-stock ETFs
  (SNDQ 2x inverse, MULL 2x long, the SanDisk complex). That's the common thread.

## Hard rules

1. **Defined-risk only.** Never sell a naked option. Every short premium position
   must have a long leg capping the loss.
2. **No leveraged or inverse single-stock ETFs.** SNDQ, MULL and similar are
   banned as underlyings — for buying, selling, or spreads.
3. **Max loss per trade ≤ 2% of account, enforced by a stop.** At ~$1,330 that is
   ~$27. A 1-wide spread's structural max loss (~$67) exceeds this, so do **not**
   hold to max loss — set a hard stop at ~1x the credit received (~$27 realized).
4. **1-wide spreads, 1 contract.** No 5-wide spreads at this account size. Width
   stays 1 point until the account is large enough (see below).
5. **Liquid large caps only.** The names that worked: AMZN, PLTR, PYPL and
   similar. These are the edge — stay inside it.
6. **Size to cash, never to margin/buying power.** If an order is rejected for
   insufficient buying power, the position was too big. (Two such rejections in
   the review: AMD spread, SNDQ put.)

## Position-size reference (account ≈ $1,330)

| Rule | Max loss / trade |
|------|-----------------:|
| 1%   | $13 |
| 2% (target) | $27 |
| 5% (never exceed) | $66 |

| Spread width | ~Credit | Max loss | % of account |
|--------------|--------:|---------:|-------------:|
| 1-wide  | ~$0.33 | ~$67  | ~5% |
| 2.5-wide | ~$0.80 | ~$170 | ~13% |
| 5-wide (banned) | ~$1.00 | ~$400 | ~30% |

## Account-size gate

At ~$1,330, one 1-wide spread held to expiry already risks ~5%, so the $27 stop
(rule 3) is mandatory. To run the proven spreads to expiry **without** a tight
stop, the account needs to be **≈ $3,300+** so one 1-wide's full max loss (~$67)
is ≤ 2%. Until then, treat this as a disciplined rebuild, not an income account.

## Pre-trade checklist

- [ ] Underlying is a liquid large cap (not a leveraged/inverse ETF)
- [ ] Structure is defined-risk (long leg caps the loss)
- [ ] Width is 1 point, 1 contract
- [ ] Hard stop set so realized loss ≤ ~$27 (2%)
- [ ] Order fits cash (no buying-power rejection)
- [ ] This trade's max loss is not larger than my average winner
