"""Unit tests for tariff parsing and the month cost model."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.falbygdens_energi.api import (
    ConsumptionPoint,
    Tariff,
    parse_price,
    parse_tariff,
)
from custom_components.falbygdens_energi.coordinator import (
    compute_hourly_profile,
    compute_month_cost,
    is_high_load_hour,
    next_period_change,
    tariff_schedule,
)

STOCKHOLM = ZoneInfo("Europe/Stockholm")
TARIFF = Tariff(
    subscription_per_year=4525.0,
    transfer_per_kwh=0.372,
    tax_per_kwh=0.45,
    peak_per_kw=45.0,
    highload_per_kw=65.0,
)


@pytest.fixture(autouse=True)
def _stockholm() -> Generator[None]:
    """Run the cost model in Swedish local time, then restore UTC for the HA plugin."""
    dt_util.set_default_time_zone(STOCKHOLM)
    yield
    dt_util.set_default_time_zone(UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4525,00 kr/år", (4525.0, "år")),
        ("37,20 öre/kWh", (0.372, "kwh")),
        ("45,00 kr/kW", (45.0, "kw")),
        ("377,08 kr/mån", (4524.96, "år")),
        ("1 234,50 kr/år", (1234.5, "år")),
        ("0,00 kr", None),
        ("", None),
    ],
)
def test_parse_price(text: str, expected: tuple[float, str] | None) -> None:
    result = parse_price(text)
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert result[0] == pytest.approx(expected[0])
        assert result[1] == expected[1]


def test_parse_tariff_prefers_vat_for_private() -> None:
    prices = [
        {"PriceLabel": "Abonnemang", "PriceVat": "4525,00 kr/år", "PriceNoVat": "3620,00 kr/år"},
        {"PriceLabel": "Elöverföring", "PriceVat": "37,20 öre/kWh", "PriceNoVat": "29,76 öre/kWh"},
        {"PriceLabel": "Effektavgift", "PriceVat": "45,00 kr/kW", "PriceNoVat": "36,00 kr/kW"},
        {"PriceLabel": "Energiskatt", "PriceVat": "45,00 öre/kWh", "PriceNoVat": "36,00 öre/kWh"},
        {"PriceLabel": "Beräknad årskostnad", "PriceVat": "0,00 kr", "PriceNoVat": "0,00 kr"},
    ]
    t = parse_tariff(prices)
    assert t.complete
    assert t.highload_per_kw is None
    assert (t.subscription_per_year, t.transfer_per_kwh, t.tax_per_kwh, t.peak_per_kw) == (
        4525.0,
        0.372,
        0.45,
        45.0,
    )
    ex = parse_tariff(prices, include_vat=False)
    assert ex.subscription_per_year == 3620.0
    assert ex.includes_vat is False


def _points(start_local: datetime, values: list[float]) -> list[ConsumptionPoint]:
    return [
        ConsumptionPoint(start=(start_local + timedelta(hours=i)).astimezone(UTC), value=v)
        for i, v in enumerate(values)
    ]


def test_month_cost_matches_invoice_formula() -> None:
    """August 2026 style month, no high load: 31 days, all hours delivered."""
    start = datetime(2026, 8, 1, tzinfo=STOCKHOLM)
    values = [1.0] * (31 * 24)
    values[18 * 24 + 1] = 11.948  # 19 Aug 01:00, like the real invoice
    now = datetime(2026, 8, 31, 23, 30, tzinfo=STOCKHOLM)
    cost = compute_month_cost(_points(start, values), TARIFF, now, cutoff=None)
    assert cost is not None
    assert cost.days_elapsed == 31 and cost.days_in_month == 31
    assert cost.peak_kw == 11.948
    assert cost.peak_at == datetime(2026, 8, 19, 1, tzinfo=STOCKHOLM)
    assert cost.highload_kw is None and cost.highload_fee == 0
    kwh = 31 * 24 - 1 + 11.948
    assert cost.kwh == pytest.approx(kwh)
    assert cost.subscription == pytest.approx(4525 * 31 / 365, abs=0.01)  # 384.31 on the invoice
    assert cost.peak_fee == pytest.approx(537.66)
    assert cost.total == pytest.approx(cost.subscription + kwh * 0.822 + 537.66, abs=0.05)
    assert cost.projected == pytest.approx(cost.total, abs=0.05)  # month complete
    assert cost.price_per_kwh == pytest.approx(cost.total / kwh, abs=1e-4)
    assert cost.projected_kwh == pytest.approx(kwh)
    assert cost.projected_price_per_kwh == pytest.approx(cost.price_per_kwh, abs=1e-4)
    assert cost.marginal_price_per_kwh == pytest.approx(0.822)


def test_month_cost_highload_excludes_weekends_nights_and_holidays() -> None:
    start = datetime(2026, 1, 1, tzinfo=STOCKHOLM)  # Thursday, New Year's Day
    values = [1.0] * (10 * 24)
    values[0 * 24 + 12] = 9.0  # Jan 1 12:00 – holiday, excluded from high load
    values[2 * 24 + 12] = 8.0  # Jan 3 (Saturday) 12:00 – weekend, excluded
    values[4 * 24 + 22] = 7.5  # Jan 5 22:00 – night, excluded
    values[6 * 24 + 8] = 6.0  # Jan 7 (Wednesday) 08:00 – counts
    now = datetime(2026, 1, 10, 12, tzinfo=STOCKHOLM)
    cost = compute_month_cost(_points(start, values), TARIFF, now, cutoff=None)
    assert cost is not None
    assert cost.in_highload_season
    assert cost.peak_kw == 9.0  # the overall peak still counts for the peak fee
    assert cost.highload_kw == 6.0
    assert cost.highload_at == datetime(2026, 1, 7, 8, tzinfo=STOCKHOLM)
    assert cost.highload_fee == pytest.approx(6.0 * 65)
    assert cost.peak_fee == pytest.approx(9.0 * 45)
    assert cost.days_elapsed == 10
    assert cost.projected is not None and cost.projected > cost.total
    # Early in the month the so-far price is inflated by the peak fees; the
    # projected price spreads them over the whole month.
    assert cost.projected_price_per_kwh is not None and cost.price_per_kwh is not None
    assert cost.projected_price_per_kwh < cost.price_per_kwh


def test_month_cost_without_tariff_still_reports_peaks() -> None:
    start = datetime(2026, 8, 1, tzinfo=STOCKHOLM)
    cost = compute_month_cost(
        _points(start, [1.0, 2.5, 0.5]), None, start + timedelta(hours=5), cutoff=None
    )
    assert cost is not None
    assert cost.peak_kw == 2.5 and cost.total == 0 and cost.price_per_kwh is None


def test_month_cost_respects_cutoff_and_ignores_previous_month() -> None:
    start = datetime(2026, 8, 31, 22, tzinfo=STOCKHOLM)
    values = [5.0, 5.0, 1.0, 1.0, 9.0]  # two hours in August, then Sept 00:00, 01:00, 02:00
    now = datetime(2026, 9, 1, 4, tzinfo=STOCKHOLM)
    cutoff = datetime(2026, 9, 1, 1, tzinfo=STOCKHOLM).astimezone(UTC)
    cost = compute_month_cost(_points(start, values), TARIFF, now, cutoff=cutoff)
    assert cost is not None
    assert cost.hours_delivered == 2 and cost.kwh == 2.0 and cost.peak_kw == 1.0


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2026, 1, 7, 8, tzinfo=STOCKHOLM), True),  # Wednesday morning in January
        (datetime(2026, 1, 7, 19, tzinfo=STOCKHOLM), False),  # window ends at 19:00
        (datetime(2026, 1, 7, 6, 59, tzinfo=STOCKHOLM), False),
        (datetime(2026, 1, 10, 12, tzinfo=STOCKHOLM), False),  # Saturday
        (datetime(2026, 1, 6, 12, tzinfo=STOCKHOLM), False),  # Epiphany (Tuesday)
        (datetime(2025, 12, 24, 12, tzinfo=STOCKHOLM), False),  # Christmas Eve (Wednesday)
        (datetime(2026, 3, 31, 12, tzinfo=STOCKHOLM), True),  # last day of the season
        (datetime(2026, 4, 1, 12, tzinfo=STOCKHOLM), False),
        (datetime(2026, 9, 11, 12, tzinfo=STOCKHOLM), False),
    ],
)
def test_is_high_load_hour(when: datetime, expected: bool) -> None:
    assert is_high_load_hour(when) is expected


def test_tariff_schedule_marks_high_load_hours_and_dst() -> None:
    day = tariff_schedule(datetime(2026, 1, 7).date(), TARIFF)
    assert len(day) == 24
    assert [h.start.hour for h in day] == list(range(24))
    assert [h.high_load for h in day] == [7 <= hour < 19 for hour in range(24)]
    assert day[8].highload_fee_per_kw == 65.0 and day[3].highload_fee_per_kw == 0.0
    assert day[8].energy_price == pytest.approx(0.822)
    # DST change day (29 March 2026) has 23 hours; it is a Sunday so no high load.
    dst = tariff_schedule(datetime(2026, 3, 29).date(), TARIFF)
    assert len(dst) == 23 and not any(h.high_load for h in dst)
    # Without a tariff the schedule still knows the periods.
    assert tariff_schedule(datetime(2026, 1, 7).date(), None)[8].energy_price is None


def test_next_period_change() -> None:
    # Friday 30 Jan 2026 at 18:30 -> window closes at 19:00.
    assert next_period_change(datetime(2026, 1, 30, 18, 30, tzinfo=STOCKHOLM)) == datetime(
        2026, 1, 30, 19, tzinfo=STOCKHOLM
    )
    # Friday evening -> next high load is Monday 07:00.
    assert next_period_change(datetime(2026, 1, 30, 20, tzinfo=STOCKHOLM)) == datetime(
        2026, 2, 2, 7, tzinfo=STOCKHOLM
    )
    # In summer -> first weekday morning of November.
    assert next_period_change(datetime(2026, 7, 1, tzinfo=STOCKHOLM)) == datetime(
        2026, 11, 2, 7, tzinfo=STOCKHOLM
    )


def test_hourly_profile_averages_per_local_hour() -> None:
    start = datetime(2026, 8, 1, tzinfo=STOCKHOLM)
    values = [float(i % 24) for i in range(3 * 24)]  # value == local hour, three days
    values[2 * 24 + 5] = 20.0  # one spike at 05:00 on day three
    now = start + timedelta(days=3)
    profile = compute_hourly_profile(
        _points(start, values), now.astimezone(UTC), cutoff=None, days=30
    )
    assert profile is not None
    assert profile.average[3] == 3.0
    assert profile.average[5] == pytest.approx((5 + 5 + 20) / 3)
    assert profile.maximum[5] == 20.0
    assert profile.heaviest_hours == [23, 22, 21]
    assert profile.lightest_hours == [0, 1, 2]
    assert profile.heaviest_hour == 23
    # Cutoff drops undelivered hours; too-old points are ignored.
    older = compute_hourly_profile(_points(start, values), now.astimezone(UTC), cutoff=None, days=1)
    assert older is not None and older.average[5] == 20.0
    assert compute_hourly_profile([], now.astimezone(UTC), cutoff=None, days=30) is None
