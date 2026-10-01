"""Reference-scenario and source-contract regression tests for `main`.

These deliberately use only the Python standard library. They test the intended
math/state invariants and check that the Pine source retains the corresponding
safeguards. They do NOT execute Pine, check TradingView types, or emulate fills.
TradingView compilation, Bar Replay, and backtests remain required.
"""

from dataclasses import dataclass
import math
from pathlib import Path
import re
import unittest


SOURCE = (Path(__file__).resolve().parents[1] / "main").read_text()
CODE = "\n".join(line.split("//", 1)[0] for line in SOURCE.splitlines())


@dataclass
class Zone:
    top: float | None
    bottom: float | None
    created: int | None
    used: bool = False

    def active(self):
        return self.top is not None and self.bottom is not None and self.created is not None

    def maintain(self, direction, close, bar, max_age=0):
        if self.active() and (
            direction * (close - (self.bottom if direction == 1 else self.top)) < 0
            or (max_age > 0 and bar - self.created > max_age)
        ):
            self.top = self.bottom = self.created = None
            self.used = False

    def retest(self, direction, bar, low, high, close, rejection=True, one_order=True):
        return (
            self.active()
            and bar > self.created
            and (not one_order or not self.used)
            and low <= self.top
            and high >= self.bottom
            and (not rejection or (close > self.top if direction == 1 else close < self.bottom))
        )


def overlap_retest(first, second, low, high):
    if not first.active() or not second.active():
        return False
    top, bottom = min(first.top, second.top), max(first.bottom, second.bottom)
    return top > bottom and low <= top and high >= bottom


def recent_bos(last_same, last_opposite, bar, memory):
    return (
        last_same is not None
        and bar - last_same <= memory
        and (last_opposite is None or last_same > last_opposite)
    )


def stop_price(direction, boundaries, wick, atr, buffer=0.25, tick=0.01):
    outer = min(boundaries) if direction == 1 else max(boundaries)
    if wick is not None:
        outer = min(outer, wick) if direction == 1 else max(outer, wick)
    raw = outer - direction * atr * buffer
    return (math.floor(raw / tick) if direction == 1 else math.ceil(raw / tick)) * tick


def target_price(direction, entry, risk, swing=None, dynamic=False, fixed_r=3.0, min_r=1.0, tick=0.01):
    ahead = dynamic and swing is not None and direction * (swing - entry) > 0
    raw = swing if ahead else entry + direction * risk * fixed_r
    round_down = (direction == 1) if ahead else (direction == -1)
    target = (math.floor(raw / tick) if round_down else math.ceil(raw / tick)) * tick
    if risk <= 0 or target <= 0 or direction * (target - entry) / risk < min_r:
        return None
    return target


def position_size(
    direction, entry, stop, *, equity=10000, risk_pct=1, point_value=1,
    fx=1, tick=0.01, slippage=0, commission=0, costs=True, exposure=0,
    step=1, whole=False,
):
    """Linear contract model: fx is the symbol-to-account currency multiplier."""
    if equity <= 0 or point_value <= 0 or fx is None or fx <= 0 or step <= 0:
        return 0.0, 0.0
    exit_slip = slippage * tick
    stop_fill = stop - direction * exit_slip
    fees = (abs(entry) + abs(stop_fill)) * commission / 100
    distance = abs(entry - stop)
    risk_unit = (distance + (exit_slip + fees if costs else 0)) * point_value * fx
    full_risk_unit = (distance + exit_slip + fees) * point_value * fx
    notional_unit = abs(entry) * point_value * fx
    if risk_unit <= 0 or notional_unit <= 0:
        return 0.0, risk_unit
    qty = equity * risk_pct / 100 / risk_unit
    if exposure > 0:
        capital_unit = notional_unit * (1 + commission / 100)
        if direction == -1:
            capital_unit += 2 * full_risk_unit
        qty = min(qty, equity * exposure / 100 / capital_unit)
    step = max(1, math.ceil(step)) if whole else step
    return math.floor(qty / step) * step, risk_unit


@dataclass
class DailyGuard:
    day: int | None = None
    baseline: float = 10000
    locked: bool = False

    def update(self, day, previous_close_equity, equity, loss_pct):
        if day != self.day:
            self.day = day
            self.baseline = previous_close_equity
            self.locked = False
        just_hit = False
        if loss_pct > 0 and not self.locked and self.baseline > 0:
            if (equity - self.baseline) / self.baseline * 100 <= -loss_pct:
                self.locked = just_hit = True
        return just_hit


class RetestTests(unittest.TestCase):
    def test_formation_bar_cannot_be_a_retest(self):
        zone = Zone(105, 100, 10)
        self.assertFalse(zone.retest(1, 10, 105, 110, 109))
        self.assertTrue(zone.retest(1, 11, 103, 110, 109))

    def test_swept_wick_with_reclaim_is_accepted(self):
        zone = Zone(105, 100, 10)
        zone.maintain(1, 106, 11)
        self.assertTrue(zone.retest(1, 11, 98, 108, 106))

    def test_close_inside_zone_is_not_a_rejection(self):
        zone = Zone(105, 100, 10)
        self.assertFalse(zone.retest(1, 11, 101, 107, 104))
        self.assertTrue(zone.retest(1, 11, 101, 107, 104, rejection=False))

    def test_bearish_retest_is_symmetric(self):
        zone = Zone(105, 100, 10)
        self.assertFalse(zone.retest(-1, 10, 95, 102, 98))
        self.assertTrue(zone.retest(-1, 11, 95, 108, 98))
        self.assertFalse(zone.retest(-1, 11, 95, 108, 102))

    def test_gap_without_range_intersection_is_not_a_touch(self):
        zone = Zone(105, 100, 10)
        self.assertFalse(zone.retest(1, 11, 106, 110, 109))
        self.assertFalse(zone.retest(-1, 11, 90, 99, 95))

    def test_used_zone_cannot_submit_again_unless_reuse_enabled(self):
        zone = Zone(105, 100, 10, used=True)
        self.assertFalse(zone.retest(1, 11, 103, 109, 107))
        self.assertTrue(zone.retest(1, 11, 103, 109, 107, one_order=False))

    def test_age_limit_and_close_invalidation(self):
        zone = Zone(105, 100, 10)
        zone.maintain(1, 106, 12, max_age=2)
        self.assertTrue(zone.active())
        zone.maintain(1, 106, 13, max_age=2)
        self.assertFalse(zone.active())
        for direction, close in [(1, 99), (-1, 106)]:
            with self.subTest(direction=direction):
                zone = Zone(105, 100, 10)
                zone.maintain(direction, close, 11)
                self.assertFalse(zone.active())

    def test_zone_age_zero_is_disabled(self):
        zone = Zone(105, 100, 10)
        zone.maintain(1, 106, 1000, max_age=0)
        self.assertTrue(zone.active())

    def test_two_disjoint_zones_do_not_boost_confluence_risk(self):
        self.assertFalse(overlap_retest(Zone(110, 105, 10), Zone(100, 95, 10), 96, 112))
        self.assertFalse(overlap_retest(Zone(110, 105, 10), Zone(105, 100, 10), 101, 112))
        self.assertTrue(overlap_retest(Zone(110, 100, 10), Zone(108, 98, 10), 102, 112))

    def test_original_trade_boundary_survives_zone_replacement_and_expiry(self):
        for direction, old_boundary, new_zone, safe_close, invalid_close in [
            (1, 100, Zone(112, 108, 20), 105, 99),
            (-1, 110, Zone(103, 100, 20), 105, 111),
        ]:
            with self.subTest(direction=direction):
                frozen_boundary = old_boundary
                new_zone.maintain(direction, safe_close, 21)
                self.assertFalse(new_zone.active())
                self.assertGreater(direction * (safe_close - frozen_boundary), 0)
                self.assertLess(direction * (invalid_close - frozen_boundary), 0)

    def test_opposite_bos_supersedes_old_memory(self):
        self.assertFalse(recent_bos(10, 12, 13, 20))
        self.assertTrue(recent_bos(12, 10, 13, 20))
        self.assertFalse(recent_bos(10, None, 31, 20))
        self.assertFalse(recent_bos(None, 10, 11, 20))
        self.assertFalse(recent_bos(10, 10, 11, 20))


class RiskAndManagementTests(unittest.TestCase):
    def test_long_stop_protects_all_zones_and_rejection_wick(self):
        stop = stop_price(1, [100, 98], wick=97, atr=2, tick=0.25)
        self.assertEqual(stop, 96.5)
        self.assertEqual(stop_price(1, [100, 98], wick=None, atr=2, tick=0.25), 97.5)

    def test_short_stop_protects_all_zones_and_rejection_wick(self):
        self.assertEqual(stop_price(-1, [105, 108], wick=109, atr=2, tick=0.25), 109.5)

    def test_stop_rounding_is_outward(self):
        self.assertLessEqual(stop_price(1, [100.03], None, 0, tick=0.25), 100.03)
        self.assertGreaterEqual(stop_price(-1, [100.03], None, 0, tick=0.25), 100.03)

    def test_fixed_target_rounding_does_not_reduce_r(self):
        for direction in (1, -1):
            target = target_price(direction, 100, 1.1, tick=0.25)
            self.assertGreaterEqual(direction * (target - 100) / 1.1, 3)

    def test_dynamic_target_can_be_nearer_than_fixed_r(self):
        self.assertEqual(target_price(1, 100, 2, swing=104, dynamic=True), 104)
        self.assertEqual(target_price(-1, 100, 2, swing=96, dynamic=True), 96)

    def test_missing_or_behind_swing_falls_back_to_fixed_r(self):
        self.assertEqual(target_price(1, 100, 2, dynamic=True), 106)
        self.assertEqual(target_price(-1, 100, 2, swing=105, dynamic=True), 94)

    def test_low_reward_or_nonpositive_target_rejects_trade(self):
        self.assertIsNone(target_price(1, 100, 2, swing=101, dynamic=True))
        self.assertIsNone(target_price(-1, 10, 5))
        self.assertIsNone(target_price(1, 100, 0))

    def test_futures_point_value_changes_quantity(self):
        qty, risk_unit = position_size(1, 5000, 4996, risk_pct=2, point_value=50)
        self.assertEqual(risk_unit, 200)
        self.assertEqual(qty, 1)  # The original price-only formula would submit 50.

    def test_symbol_loss_is_converted_to_account_currency(self):
        qty, risk_unit = position_size(1, 100, 95, fx=1.25)
        self.assertEqual(risk_unit, 6.25)
        self.assertEqual(qty, 16)

    def test_cost_budget_includes_exit_slippage_and_two_sided_commission(self):
        qty, risk_unit = position_size(1, 100.02, 98, slippage=2, commission=0.02)
        self.assertAlmostEqual(risk_unit, 2.0796)
        self.assertEqual(qty, 48)
        self.assertLessEqual(qty * risk_unit, 100)
        no_cost_qty, _ = position_size(1, 100.02, 98, slippage=2, commission=0.02, costs=False)
        self.assertEqual(no_cost_qty, 49)

    def test_fractional_steps_are_rounded_down(self):
        qty, risk_unit = position_size(1, 100, 97, step=0.25)
        self.assertEqual(qty, 33.25)
        self.assertLessEqual(qty * risk_unit, 100)
        self.assertEqual(position_size(1, 100, 97, step=0.25, whole=True)[0], 33)

    def test_too_small_quantity_is_zero_not_rounded_up(self):
        self.assertEqual(position_size(1, 100, 98, equity=100, point_value=50)[0], 0)

    def test_missing_conversion_or_invalid_capital_blocks_quantity(self):
        for overrides in ({"equity": 0}, {"equity": -100}, {"point_value": 0}, {"fx": None}, {"step": 0}):
            with self.subTest(overrides=overrides):
                self.assertEqual(position_size(1, 100, 98, **overrides)[0], 0)

    def test_exposure_cap_leaves_room_for_entry_commission(self):
        qty, _ = position_size(1, 100, 99, risk_pct=3, exposure=100, commission=0.02)
        self.assertEqual(qty, 99)
        self.assertLessEqual(qty * 100 * 1.0002, 10000)

    def test_unlevered_short_has_margin_headroom_until_estimated_stop(self):
        qty, risk_unit = position_size(-1, 100, 101, risk_pct=3, exposure=100)
        self.assertEqual(qty, 98)
        equity_at_stop = 10000 - qty * risk_unit
        required_margin_at_stop = qty * 101
        self.assertLessEqual(required_margin_at_stop, equity_at_stop)

    def test_short_margin_reserve_keeps_costs_even_when_risk_cost_toggle_is_off(self):
        qty, _ = position_size(-1, 100, 101, risk_pct=3, exposure=100, slippage=2, commission=0.02, costs=False)
        self.assertEqual(qty, 97)

    def test_managed_stops_never_loosen(self):
        long_stop, short_stop = 95, 105
        for long_candidate, short_candidate in [(100, 100), (98, 102), (103, 97)]:
            next_long = max(long_stop, long_candidate)
            next_short = min(short_stop, short_candidate)
            self.assertGreaterEqual(next_long, long_stop)
            self.assertLessEqual(next_short, short_stop)
            long_stop, short_stop = next_long, next_short

    def test_newly_crossed_stop_requires_current_market_price_not_retroactive_be_fill(self):
        for direction, new_stop, close in [(1, 100, 99), (-1, 100, 101)]:
            with self.subTest(direction=direction):
                self.assertLessEqual(direction * (close - new_stop), 0)
                self.assertNotEqual(close, new_stop)


class DailyGuardTests(unittest.TestCase):
    def test_guard_latches_after_recovery_and_alerts_once(self):
        guard = DailyGuard()
        self.assertTrue(guard.update(1, 10000, 9700, 2))
        self.assertFalse(guard.update(1, 9700, 10100, 2))
        self.assertTrue(guard.locked)
        self.assertFalse(guard.update(1, 10100, 9600, 2))

    def test_first_bar_of_new_day_includes_gap_loss(self):
        guard = DailyGuard(day=1)
        self.assertTrue(guard.update(2, 10000, 9700, 2))
        self.assertEqual(guard.baseline, 10000)

    def test_full_trading_day_key_resets_across_months(self):
        guard = DailyGuard()
        guard.update(20260701, 10000, 9700, 2)
        self.assertFalse(guard.update(20260801, 9700, 9700, 2))
        self.assertFalse(guard.locked)

    def test_exchange_trading_day_not_calendar_midnight_controls_reset(self):
        guard = DailyGuard()
        guard.update(100, 10000, 9700, 2)
        guard.update(100, 9700, 10000, 2)  # Same overnight session after midnight.
        self.assertTrue(guard.locked)
        guard.update(101, 10000, 10000, 2)
        self.assertFalse(guard.locked)

    def test_disabled_daily_guard_does_not_lock(self):
        guard = DailyGuard()
        self.assertFalse(guard.update(1, 10000, 9000, 0))
        self.assertFalse(guard.locked)

    def test_exit_bar_and_configured_extra_cooldown_are_blocked(self):
        self.assertFalse(5 - 5 > 0)
        self.assertTrue(6 - 5 > 0)
        self.assertFalse(6 - 5 > 1)
        self.assertTrue(7 - 5 > 1)


class PineSourceContractTests(unittest.TestCase):
    def test_confirmed_close_execution_mode_and_zero_pyramiding(self):
        self.assertTrue(SOURCE.startswith("//@version=6\n"))
        for setting in ("calc_on_every_tick", "calc_on_order_fills"):
            self.assertRegex(CODE, rf"{setting}\s*=\s*false")
        self.assertRegex(CODE, r"process_orders_on_close\s*=\s*true")
        self.assertRegex(CODE, r"pyramiding\s*=\s*0")

    def test_htf_request_is_shifted_and_missing_values_fail_closed(self):
        self.assertEqual(CODE.count("request.security("), 1)
        self.assertIn("ta.ema(close, htfEmaLength)[1]", CODE)
        self.assertIn("lookahead=barmerge.lookahead_on", CODE)
        self.assertIn("timeframe.in_seconds(htfTimeframe) <= timeframe.in_seconds()", CODE)
        self.assertIn("not useHtfFilter or (not na(htfEma) and close > htfEma)", CODE)
        self.assertIn("not useHtfFilter or (not na(htfEma) and close < htfEma)", CODE)

    def test_zone_maintenance_and_entries_precede_new_zone_creation(self):
        self.assertLess(SOURCE.index("bullFvg.maintain(true)"), SOURCE.index("// ---------------- Retest / Signal Grading"))
        self.assertLess(SOURCE.index("strategy.entry("), SOURCE.index("// ---------------- New Zones"))
        self.assertIn("bar_index > this.createdBar", CODE)
        self.assertIn("low <= this.top and high >= this.bottom", CODE)
        self.assertIn("overlapTop > overlapBottom", CODE)

    def test_trade_exit_uses_frozen_boundaries_not_replaced_zone_fields(self):
        management = SOURCE.split("// ---------------- Open Trade Management")[1].split("// ---------------- Retest / Signal Grading")[0]
        self.assertIn("close - tradeFvgBoundary", management)
        self.assertIn("close - tradeObBoundary", management)
        self.assertNotRegex(management, r"(?:bull|bear)(?:Fvg|Ob)\.")
        self.assertEqual(CODE.count("tradeFvgBoundary :="), 2)  # Flat reset and submission only.
        self.assertEqual(CODE.count("tradeObBoundary :="), 2)
        self.assertLess(CODE.index("if strategy.position_size == 0"), CODE.index("if signalDirection != 0"))

    def test_size_uses_point_value_currency_costs_and_downward_rounding(self):
        helper = SOURCE.split("f_positionSize(", 1)[1].split("// ---------------- State / Indicators", 1)[0]
        for guard in (
            "strategy.convert_to_account", "syminfo.pointvalue", "syminfo.mincontract",
            "estimatedStopFill", "commissionDistance", "2.0 * fullStopRiskPerUnit",
            "math.floor(qty / step) * step", "strategy.equity > 0",
        ):
            self.assertIn(guard, helper)
        self.assertLess(CODE.index("if not na(qty) and qty > 0"), CODE.index("bullFvg.consume()"))

    def test_actual_fill_reconciliation_and_entry_bar_protection(self):
        self.assertIn("tradeInitRisk := positionDirection * (entryPrice - tradeStop)", CODE)
        self.assertIn("bar_index > tradeEntryBar", CODE)
        self.assertIn('managedStopCrossed ? "Managed Stop Crossed"', CODE)
        self.assertIn("tradeStop := positionDirection == 1 ? math.max", CODE)
        self.assertIn(": math.min(tradeStop, trailingStop)", CODE)

    def test_daily_guard_reset_and_latch_contract(self):
        self.assertIn("time_tradingday != time_tradingday[1]", CODE)
        self.assertIn("dayStartEquity := nz(strategy.equity[1], strategy.initial_capital)", CODE)
        self.assertEqual(CODE.count("dailyLossLocked := false"), 1)
        self.assertEqual(CODE.count("dailyLossLocked := true"), 1)
        self.assertIn("and not dailyLossLocked", CODE)
        self.assertIn("if dailyLossJustHit and enableRiskAlerts", CODE)

    def test_strategy_orders_have_fill_messages_and_entries_have_brackets(self):
        self.assertNotIn("alertcondition(", CODE)
        calls = re.findall(r"strategy\.(?:entry|exit|close|close_all)\([^\n]*", CODE)
        self.assertEqual(len(calls), 9)
        self.assertTrue(all("alert_message=" in call for call in calls))
        for side in ("Long", "Short"):
            self.assertRegex(CODE, rf'strategy.entry\("{side}"[^\n]*\n\s*strategy.exit\("{side} Exit", "{side}"')
        self.assertIn("//@strategy_alert_message {{strategy.order.alert_message}}", SOURCE)

    def test_session_date_bounds_and_exit_bar_cooldown(self):
        self.assertIn("time_close <= sessionBarClose", CODE)
        self.assertIn("time_close <= endDateIn", CODE)
        self.assertIn("bar_index - lastExitBar > cooldownBars", CODE)
        self.assertIn("lastBullBOSBar > lastBearBOSBar", CODE)
        self.assertIn("lastBearBOSBar > lastBullBOSBar", CODE)


if __name__ == "__main__":
    unittest.main()
