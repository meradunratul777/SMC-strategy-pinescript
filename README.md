# SMC strategy — Pine Script v6

The TradingView strategy is in [`main`](main). The filename is intentionally unchanged; copy its complete contents into the TradingView Pine Editor, save, and add it to a **standard, time-based candlestick chart**.

This revision fixes execution/state problems and adds configurable entry-quality checks. It does **not** establish that the strategy is profitable or that the changed defaults outperform the original.

## Strategy understood and retained

- **Structure:** `ta.pivothigh` / `ta.pivotlow` confirm swings after `Swing Length` bars. A closing-price break consumes that swing level and records a bullish/bearish BOS. The confirmation delay is intentional; BOS markers are not plotted back on the pivot candle.
- **FVG:** the original three-candle gap: bullish `low > high[2]`, bearish `high < low[2]`, with an optional minimum percentage gap.
- **Order block:** the original simplified proxy: an opposing previous candle followed by a close beyond its high/low, with an impulse body larger than that previous candle's full range. This is **not automatically a BOS-qualified order block**; `Require BOS on Order Block Formation` can enforce that relationship.
- **Entries:** retest a bullish zone with a bullish candle, or a bearish zone with a bearish candle; optionally filter by EMA, confirmed HTF EMA, session, date, ATR percentage, and recent directional BOS.
- **Risk tiers:** 1% for a single signal, 2% for spatial FVG/OB confluence or an enabled recent BOS boost, as before. A BOS boost is a heuristic, not evidence that doubling risk is justified.
- **Exits:** an ATR-buffered structural stop, default 3R target, optional swing target, original-zone invalidation, gross break-even, optional ATR trailing, and optional daily-loss flattening.
- Still **one displayed zone per direction/type**, one open position, and no pyramiding. This is not a new multi-zone or mandatory liquidity-sweep strategy.

## Audit and implemented changes

| Finding in the original | Implemented correction / behavior |
| --- | --- |
| A new bullish FVG's `low` equals its top, so it could pass the touch check on the formation bar. OB impulses could also enter before a retest. | Maintain/evaluate existing zones first; create new zones after trade decisions. Explicitly require `bar_index > createdBar` for every retest. |
| Replacing an active FVG/OB changed what could invalidate an existing trade, because trades only remembered the zone type. | Snapshot the actual triggering boundaries at submission. Replacement, consumption, or age expiry of displayed zones cannot change an open trade's invalidation thresholds. |
| The touch test missed wicks that swept through a zone and reclaimed it, while a green/red candle could still close inside the zone. | Test candle/zone **range intersection**. By default require a close above the bullish zone's top or below the bearish zone's bottom, plus the original candle direction. Invalidation remains close-based. |
| Used zones could repeatedly generate entries indefinitely. | Default to one valid entry **order attempt** per zone, with used zones gray. Optional age expiry. Invalid plans/zero quantity do not consume a zone. |
| A wide candle touching two disjoint zones doubled risk as “confluence.” Both bullish and bearish BOS memories could remain fresh. | Confluence requires actual overlapping zone area touched by the candle. Only the latest BOS direction can qualify for a risk boost or directional entry filter. |
| A confluence trade used the tighter boundary, possibly inside another triggering zone. Rejection wicks could extend through that stop. | Always protect the outer boundary of every triggering zone; include the entry wick by default, then add the ATR buffer and round the stop outward to a valid tick. |
| `riskAmount / priceDistance` ignored contract point value, account currency, transaction costs, and quantity increments. | Include `syminfo.pointvalue`, `strategy.convert_to_account`, estimated costs, and downward quantity-step rounding. Add a notional cap rather than relying on oversized orders being rejected. |
| Risk/targets were based on the signal close despite entry slippage. | Plan from an expected slippage-adjusted entry; attach the stop/target with the entry order. Reconcile initial price R and fixed target to `strategy.position_avg_price` on the next regular calculation. |
| “Swing target” only selected a swing beyond the fixed-R target. | When enabled, use the latest confirmed swing ahead of entry, even if nearer than fixed R. Reject a too-close target using `Minimum Acceptable Target`; missing/behind swings fall back to fixed R. |
| `lookahead_off` alone still exposed a developing HTF EMA in realtime; unavailable HTF data bypassed the filter. | Request `ta.ema(...)[1]` with `lookahead_on`: the **previous closed HTF bar**, not future data. Reject non-higher timeframes and block entries when confirmed HTF data is missing. |
| The flat-state reset depended on a latch that was also set on unsuccessful entry attempts. | Reset trade state **before** entry evaluation and write it only for valid submitted orders. Share the long/short entry path to keep risk calculations symmetric. |
| Daily-loss blocking could unlock after equity recovered; day-of-month reset and first-bar equity missed some day/gap losses. | Latch until `time_tradingday` changes. Use prior-close equity as the new day's baseline, including the first bar's gap/open P&L; alert once on the threshold crossing. |
| `alertcondition()` does not provide selectable strategy alerts; stop/target fills were not represented by the exit flags. | Add custom messages on every order-producing call for **order-fill alerts**, plus optional `alert()` submission messages and a daily-loss event. Submission and execution are explicitly distinguished. |

Other safeguards: wait for chart EMA warm-up when enabled; require the full entry bar inside the selected session/date range; validate contradictory input bounds; and never reuse an exit bar's potentially pre-exit wick for a new entry. Session/date/volatility filters affect **entries only**—protective exits remain enabled outside those filters.

## New defaults that change trades

These are deliberate choices to test, not guaranteed edge improvements:

- `Require Close Back Outside Entry Zone = true`.
- `One Entry Order per Zone = true` (marked on submission, not proof of a fill).
- `Place Stop Beyond Entry Candle Wick = true`, with the outer confluence boundary always used.
- `Budget Estimated Costs in Position Risk = true`.
- `Max Position Notional = 100%` of equity, with estimated entry commission reserved. Shorts also reserve estimated loss/margin headroom: at 100% margin, a rising price both reduces equity and increases required notional collateral.
- `Confirm BE/Trailing Activation on Close = true`. Trailing is still off by default; if enabled, it starts at 1R rather than immediately.
- `Minimum Acceptable Target = 1R`. If testing a fixed target below 1R, lower this input too; it cannot exceed the fixed-R fallback.

Zone-age expiry (`0 = off`), minimum ATR displacement (`0 = off`), BOS-qualified OB formation, and mandatory recent BOS entries remain optional/off. Recent BOS memory is still 20 bars. The exit bar itself is always blocked; `Extra Bars to Wait After Exit = 0` permits the next bar.

You can disable rejection, one-order consumption, wick inclusion, or close-confirmed management independently to compare variants. Formation-bar entries, mutable trade boundaries, repainting HTF requests, and price-only sizing are not restored by those toggles.

## Position sizing and execution assumptions

For a linear instrument, the planned size is approximately:

```text
expectedEntry = signalClose + direction × estimatedSlippageTicks × tickSize
initialPriceR = direction × (expectedEntry − structuralStop)
expectedStopFill = structuralStop − direction × estimatedSlippageTicks × tickSize
commissionDistance = (|expectedEntry| + |expectedStopFill|) × commissionPercent / 100
riskPerUnit = convert_to_account(
    (initialPriceR + estimatedExitSlippage + commissionDistance) × pointValue
)
quantity = floor_to_step(min(equity × riskPercent / 100 / riskPerUnit, exposureCapQuantity))
```

The cost terms are omitted from the **risk budget** when cost budgeting is disabled; expected entry slippage still affects the entry/stop distance. The exposure cap retains its estimated cost/margin reserve. Quantity rounding and the exposure cap can reduce actual planned risk below the selected tier. R targets and activation thresholds use **initial price risk**, not commission-adjusted net R.

Important setup details:

1. **Match Inputs to Strategy Properties.** `Sizing Slippage = 2 ticks` and `Sizing Commission = 0.02% per side` match the strategy declaration. Pine does not expose those user-overridden Properties to this sizing calculation. Changing these Inputs does not change simulated fees/slippage. The cost model assumes percentage commission; cash-per-contract fees need a different model.
2. **Margin defaults are 100% in both directions.** Expensive futures can legitimately produce zero contracts with $10,000 and no leverage. If testing leverage, explicitly set compatible Properties margins and an appropriate notional cap, allowing additional headroom. The script does not infer those changed margin settings, and its short reserve is designed around the unlevered default.
3. **Quantity Step** defaults to `syminfo.mincontract`, which is a symbol minimum, not a universal broker increment. Override it for your broker. Whole-unit rounding turns the resolved step into a whole-number step; use whole steps for stocks/futures. If the risk budget cannot buy one increment, the trade is skipped rather than rounded up.
4. This sizing model assumes linear price/contract P&L and valid symbol-to-account currency conversion. Check unusual, inverse, or nonlinear contracts separately against your broker's specifications. Missing conversion data blocks sizing.
5. Leave **Recalculate On Every Tick** and **After Order Is Filled** disabled. Orders are close-confirmed, with `process_orders_on_close = true`. Stops/limits are submitted alongside entries, but cannot protect the part of the entry candle before a closing entry exists. Actual fills can differ from estimates; reconciliation does not retroactively resize a filled trade.
6. BE/trailing decisions and the custom daily guard are evaluated **at bar close**, not continuously intrabar. A newly raised stop already crossed at that close submits an immediate market close at the available market price, not a fictitious earlier BE/trailing fill. Previously submitted protective stops/limits remain broker-emulator orders.
7. “Gross break-even” means moving the stop to approximately entry price; commissions and exit slippage can still make the exit a net loss. Trailing stops only tighten and follow the best favorable high/low observed **after** the entry bar.
8. Daily loss is measured from the preceding close, not the intraday equity peak. The lock includes open P&L and persists even if `Flatten on Daily Loss Hit` is disabled. It follows the instrument's trading-day key, including overnight sessions. The check is sampled at closes, so **it is not a hard maximum loss guarantee**. Use intraday charts for meaningful intraday monitoring; weekly/monthly charts are rejected when this guard is enabled.
9. Gaps, slippage beyond estimates, margin calls, liquidity, and currency-rate changes can exceed the planned risk. Historical emulator fills and closing-session execution are not a guarantee of live broker fills. Test with realistic Properties and appropriate intrabar detail.

## Alerts

Create a TradingView alert on the strategy:

- Choose **Order fills only** for actual emulator executions. The default message directive uses `{{strategy.order.alert_message}}`; retain that placeholder if editing the alert message.
- Choose **Order fills and alert() function calls** to include the daily-loss notification. `Enable Daily-Loss Alert` defaults to on.
- `Enable Order-Submission Alerts` defaults to off to avoid duplicate submission/fill notifications. If enabled, these messages say **ORDER SUBMITTED** and are not execution confirmations.
- Recreate existing alerts after changing the script, Inputs, symbol, timeframe, or Properties. Running alerts keep a saved snapshot.

## Validation

Run the dependency-free reference-scenario and source-contract suite:

```bash
python3 -m unittest discover -s tests -v
git diff --check
```

The 44 tests cover formation-bar rejection, swept-wick entries, bearish symmetry, zone consumption/age, immutable entry boundaries, spatial confluence, BOS memory, stops/targets, contract/currency sizing, estimated costs, quantity/exposure limits, stop monotonicity, and daily-lock/reset behavior. These tests **do not execute Pine or TradingView's broker emulator** and are not performance backtests.

A supplementary syntax parse was also performed using the third-party PyneScript 0.3.0 parser. To repeat that optional check in your own Python environment:

```bash
pip install pynescript==0.3.0
pynescript parse-and-dump main > /tmp/smc-main-ast.txt
```

This checks grammar only, not TradingView API signatures/type semantics. **TradingView compilation and Strategy Tester runs have not been performed in this repository environment.**

### TradingView verification checklist

1. Compile `main` in Pine Editor and add it to a standard time-based chart. Keep the documented execution Properties.
2. In Bar Replay, verify no FVG/OB entry on its formation bar. A box may start on its earlier source candle for visualization; it was only known at the formation bar's close.
3. Check rejection closes and wick sweeps in both directions. Used zones should gray out; invalid plans should not consume them. Exercise optional age/displacement/BOS filters one at a time.
4. Hold a trade while a newer zone replaces its displayed entry zone. Exit-on-invalidation must still refer to the **original** boundaries, not the replacement or an expired drawing.
5. Enable a genuinely higher HTF. Verify the confirmed EMA changes only as closed HTF values become available and does not change after chart reload. Equal/lower HTFs should raise an input error.
6. Check long/short quantities on a stock, a point-value-multiplier futures contract, and a symbol quoted in a different account currency. Verify Properties and broker step assumptions; confirm insufficient capital produces no order.
7. Test fixed and dynamic targets, including a nearer acceptable swing and an insufficient-R swing. Verify the actual fill/stop distance used by BE/trailing.
8. Trigger BE/trailing and verify stops never loosen. Test a wick-triggered newly crossed stop: it must exit at the closing market fill, not assume a favorable earlier stop fill.
9. With daily flatten both on and off, hit the daily threshold, recover equity, and verify no new entry until the next trading day. Include an overnight session and a first-bar gap loss.
10. Verify exit-bar cooldown, EMA warm-up, session-end/date boundaries, and order-fill/daily-loss alerts in realtime/paper trading.

For performance comparison, use the same symbol, timeframe, date range, capital, margin, and realistic costs for both versions. Keep a separate out-of-sample period, compare drawdown, net expectancy, trade count, and long/short results—not only win rate—and change one optional filter at a time. Validate with Bar Magnifier where available and then forward/paper test. No parameters were optimized against price data in this review.

## Relevant TradingView documentation

- [Confirmed HTF requests and repainting](https://www.tradingview.com/pine-script-docs/concepts/other-timeframes-and-data/)
- [Strategy orders, fills, currency conversion, margin, and execution settings](https://www.tradingview.com/pine-script-docs/concepts/strategies/)
- [Symbol point value, tick size, and minimum contract quantity](https://www.tradingview.com/pine-script-docs/concepts/chart-information/)
- [Strategy alerts and order-fill messages](https://www.tradingview.com/pine-script-docs/concepts/alerts/)
- [Trading-day timestamps and overnight sessions](https://www.tradingview.com/pine-script-docs/concepts/time/)
