"""Data update coordinator for Falbygdens Energi."""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    AuthenticationError,
    CannotConnectError,
    ConsumptionModel,
    ConsumptionPoint,
    FalbygdensEnergiClient,
    Interval,
    Invoice,
    MeterInfo,
    MeterReading,
    PortalInfo,
    Site,
    Tariff,
)
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    STATISTICS_BACKFILL_DAYS,
    STATISTICS_REFRESH_DAYS,
)

_LOGGER = logging.getLogger(__name__)

type FalbygdensEnergiConfigEntry = ConfigEntry[FalbygdensEnergiCoordinator]

HIGHLOAD_MONTHS = {11, 12, 1, 2, 3}
HIGHLOAD_HOURS = range(7, 19)  # weekdays 07:00–19:00 local


def _swedish_holidays(year: int) -> set[date]:
    """Public holidays plus the eves Falbygdens Energi excludes from high load."""
    days: set[date] = set()
    try:
        import holidays  # noqa: PLC0415

        days.update(holidays.Sweden(years=[year], include_sundays=False))
    except Exception:  # noqa: BLE001 - fall back to the fixed-date ones
        days.update(
            {
                date(year, 1, 1),
                date(year, 1, 6),
                date(year, 5, 1),
                date(year, 6, 6),
                date(year, 12, 25),
                date(year, 12, 26),
            }
        )
    days.update({date(year, 12, 24), date(year, 12, 31)})
    return days


@dataclass(slots=True)
class MonthCost:
    """Grid cost of the current month so far, computed from hourly values and the tariff."""

    month: date
    days_elapsed: int
    days_in_month: int
    hours_delivered: int
    kwh: float
    peak_kw: float | None = None
    peak_at: datetime | None = None
    highload_kw: float | None = None
    highload_at: datetime | None = None
    subscription: float = 0.0
    transfer: float = 0.0
    tax: float = 0.0
    peak_fee: float = 0.0
    highload_fee: float = 0.0
    total: float = 0.0
    projected: float | None = None
    price_per_kwh: float | None = None

    @property
    def in_highload_season(self) -> bool:
        """True from November through March."""
        return self.month.month in HIGHLOAD_MONTHS


@dataclass(slots=True)
class InvoicePrice:
    """One invoiced month: what it cost and what that made a kWh."""

    period: date
    invoice: Invoice
    kwh: float | None
    price_per_kwh: float | None


@dataclass(slots=True)
class SiteData:
    """Everything fetched for one site (use place) in a refresh."""

    site: Site
    unit: str = UnitOfEnergy.KILO_WATT_HOUR
    hourly: list[ConsumptionPoint] = field(default_factory=list)
    today: float | None = None
    yesterday: float | None = None
    month_to_date: float | None = None
    year_to_date: float | None = None
    last_year_to_date: float | None = None
    last_hour_start: datetime | None = None
    meter_stand: float | None = None
    meter_stand_date: datetime | None = None
    meter_stand_meter_id: str | None = None
    # Month start (local date) -> kWh, for the current and, if needed, previous year.
    monthly: dict[date, float] = field(default_factory=dict)
    use_place_code: str | None = None
    # Settled prices from invoices (newest first) and the live month estimate.
    invoiced: list[InvoicePrice] = field(default_factory=list)
    tariff: Tariff | None = None
    month_cost: MonthCost | None = None

    @property
    def last_invoiced(self) -> InvoicePrice | None:
        """Newest invoiced month."""
        return self.invoiced[0] if self.invoiced else None

    @property
    def primary_meter(self) -> MeterInfo | None:
        """The meter used for identifiers (first hourly one, else first)."""
        return next((m for m in self.site.meters if m.is_hourly), None) or (
            self.site.meters[0] if self.site.meters else None
        )


@dataclass(slots=True)
class InvoiceSummary:
    """Aggregated view of the invoice list."""

    invoices: list[Invoice] = field(default_factory=list)

    @property
    def unpaid_amount(self) -> float:
        """Sum of what is still to pay."""
        return round(sum(i.remaining_amount for i in self.invoices), 2)

    @property
    def unpaid_count(self) -> int:
        """Number of invoices with a remaining amount."""
        return sum(1 for i in self.invoices if i.remaining_amount > 0)

    @property
    def overdue_amount(self) -> float:
        """Sum of remaining amounts on overdue invoices."""
        return round(sum(i.remaining_amount for i in self.invoices if i.overdue), 2)

    @property
    def latest(self) -> Invoice | None:
        """Most recent invoice."""
        return self.invoices[0] if self.invoices else None

    @property
    def next_due_date(self) -> date | None:
        """Earliest due date among unpaid invoices."""
        dates = [i.due_date for i in self.invoices if i.remaining_amount > 0 and i.due_date]
        return min(dates) if dates else None


@dataclass(slots=True)
class PortalData:
    """Everything a single refresh produced."""

    info: PortalInfo
    portal_version: str | None
    fetched_at: datetime
    sites: list[SiteData] = field(default_factory=list)
    invoices: InvoiceSummary = field(default_factory=InvoiceSummary)


def _sum_points(points: list[ConsumptionPoint], start: datetime, end: datetime) -> float | None:
    """Sum hourly points with ``start <= point.start < end``; None if no points."""
    selected = [p.value for p in points if start <= p.start < end]
    return round(sum(selected), 3) if selected else None


def _last_nonzero_hour(points: list[ConsumptionPoint], now: datetime) -> datetime | None:
    """Return the start of the latest hour that has a real (non-zero) value.

    The portal returns 0 for hours it has not received yet, so the last
    non-zero hour before now is the best estimate of "data up to".
    """
    for point in reversed(points):
        if point.start < now and point.value != 0:
            return point.start
    return None


def compute_month_cost(
    points: list[ConsumptionPoint],
    tariff: Tariff | None,
    now_local: datetime,
    *,
    cutoff: datetime | None,
) -> MonthCost | None:
    """Price the current month from its delivered hourly values.

    Mirrors the invoice: subscription × days/365, kWh × (transfer + tax),
    highest hour × peak fee and, November–March, highest weekday 07–19 hour
    (holidays and eves excluded) × high-load fee.  ``projected`` scales the
    energy part to the full month and keeps the peak charges as they stand.
    """
    month_start = dt_util.start_of_local_day(now_local).replace(day=1)
    days_in_month = calendar.monthrange(now_local.year, now_local.month)[1]
    holidays_ = _swedish_holidays(now_local.year)
    delivered = [
        p
        for p in points
        if p.start >= month_start and (cutoff is None or p.start <= cutoff) and p.start.minute == 0
    ]
    if not delivered:
        return None

    cost = MonthCost(
        month=month_start.date(),
        days_elapsed=now_local.day,
        days_in_month=days_in_month,
        hours_delivered=len(delivered),
        kwh=round(sum(p.value for p in delivered), 3),
    )
    peak = max(delivered, key=lambda p: p.value)
    cost.peak_kw, cost.peak_at = round(peak.value, 3), peak.start
    if cost.in_highload_season:
        candidates = []
        for p in delivered:
            local = dt_util.as_local(p.start)
            if (
                local.weekday() < 5
                and local.hour in HIGHLOAD_HOURS
                and local.date() not in holidays_
            ):
                candidates.append(p)
        if candidates:
            top = max(candidates, key=lambda p: p.value)
            cost.highload_kw, cost.highload_at = round(top.value, 3), top.start
        else:
            cost.highload_kw = 0.0

    if tariff is None or not tariff.complete:
        return cost

    assert tariff.subscription_per_year is not None
    assert tariff.transfer_per_kwh is not None
    assert tariff.tax_per_kwh is not None
    assert tariff.peak_per_kw is not None
    cost.subscription = round(tariff.subscription_per_year * cost.days_elapsed / 365, 2)
    cost.transfer = round(cost.kwh * tariff.transfer_per_kwh, 2)
    cost.tax = round(cost.kwh * tariff.tax_per_kwh, 2)
    cost.peak_fee = round((cost.peak_kw or 0) * tariff.peak_per_kw, 2)
    if cost.in_highload_season and tariff.highload_per_kw is not None:
        cost.highload_fee = round((cost.highload_kw or 0) * tariff.highload_per_kw, 2)
    cost.total = round(
        cost.subscription + cost.transfer + cost.tax + cost.peak_fee + cost.highload_fee, 2
    )
    if cost.kwh > 0:
        cost.price_per_kwh = round(cost.total / cost.kwh, 4)
        hours_in_month = days_in_month * 24
        energy_full = (cost.transfer + cost.tax) * hours_in_month / cost.hours_delivered
        cost.projected = round(
            tariff.subscription_per_year * days_in_month / 365
            + energy_full
            + cost.peak_fee
            + cost.highload_fee,
            2,
        )
    return cost


class FalbygdensEnergiCoordinator(DataUpdateCoordinator[PortalData]):
    """Poll the portal on a slow schedule and expose the result."""

    config_entry: FalbygdensEnergiConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: FalbygdensEnergiConfigEntry,
        client: FalbygdensEnergiClient,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client
        self._statistics_imported: set[str] = set()

    # ------------------------------------------------------------------ refresh
    async def _async_update_data(self) -> PortalData:
        """Fetch fresh data from the portal."""
        try:
            version = await self.client.async_get_version()
            tariffs = await self._async_safe_tariffs()
            model = await self.client.async_load_consumption_model()
            sites = [
                await self._async_fetch_site(model, site, tariffs.get(site.site_id))
                for site in model.sites
            ]
            readings = await self._async_safe_meter_readings()
            invoices = await self._async_safe_invoices()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except CannotConnectError as err:
            raise UpdateFailed(str(err)) from err

        for site_data in sites:
            self._apply_meter_stand(site_data, readings)
            site_data.use_place_code = self.client.use_place_codes.get(site_data.site.site_id)
            try:
                await self._async_apply_invoiced_prices(
                    model, site_data, invoices, len(model.sites)
                )
            except CannotConnectError as err:
                _LOGGER.debug("Invoice prices for %s unavailable: %s", site_data.site.name, err)

        data = PortalData(
            info=self.client.info,
            portal_version=version.get("FileVersionServer") if isinstance(version, dict) else None,
            fetched_at=dt_util.utcnow(),
            sites=sites,
            invoices=InvoiceSummary(invoices),
        )

        for site_data in sites:
            try:
                await self._async_import_statistics(site_data)
            except Exception:  # noqa: BLE001 - statistics must never break the sensors
                _LOGGER.exception("Importing statistics for %s failed", site_data.site.name)

        return data

    async def _async_fetch_site(
        self, model: ConsumptionModel, site: Site, tariff: Tariff | None
    ) -> SiteData:
        """Fetch hourly, monthly and year data for one site."""
        now_local = dt_util.now()
        today = now_local.date()
        first_run = self._statistic_id(site) not in self._statistics_imported
        days = STATISTICS_BACKFILL_DAYS if first_run else STATISTICS_REFRESH_DAYS
        # Always cover the whole current month so the month cost can be computed.
        hourly_start = min(today - timedelta(days=days), today.replace(day=1))

        hourly = await self.client.async_get_consumption(
            model, site, hourly_start, today, Interval.HOUR
        )
        yearly = await self.client.async_get_consumption(
            model, site, date(today.year, 1, 1), date(today.year, 12, 31), Interval.MONTH
        )

        midnight = dt_util.start_of_local_day(now_local)
        month_start = midnight.replace(day=1)
        data = SiteData(site=site, unit=hourly.unit or UnitOfEnergy.KILO_WATT_HOUR, tariff=tariff)
        data.hourly = hourly.points
        data.today = _sum_points(hourly.points, midnight, midnight + timedelta(days=1))
        data.yesterday = _sum_points(hourly.points, midnight - timedelta(days=1), midnight)
        data.last_hour_start = _last_nonzero_hour(hourly.points, dt_util.utcnow())
        data.month_cost = compute_month_cost(
            hourly.points, tariff, now_local, cutoff=data.last_hour_start
        )

        # Month to date from the monthly series (covers the whole month even
        # if the hourly window is shorter), year to date from CompareModel.
        data.monthly = {
            dt_util.as_local(p.start).date().replace(day=1): round(p.value, 3)
            for p in yearly.points
        }
        data.month_to_date = data.monthly.get(month_start.date())
        data.year_to_date = (
            yearly.current_period_total if yearly.current_period_total is not None else yearly.total
        )
        data.last_year_to_date = yearly.last_year_period_total
        return data

    async def _async_safe_meter_readings(self) -> dict[str, list[MeterReading]]:
        try:
            return await self.client.async_get_meter_readings()
        except CannotConnectError as err:
            _LOGGER.debug("Meter readings unavailable: %s", err)
            return {}

    async def _async_safe_invoices(self) -> list[Invoice]:
        try:
            return await self.client.async_get_invoices()
        except CannotConnectError as err:
            _LOGGER.debug("Invoices unavailable: %s", err)
            return []

    @staticmethod
    def _apply_meter_stand(site_data: SiteData, readings: dict[str, list[MeterReading]]) -> None:
        """Pick the newest real meter stand for the site's meters."""
        best: MeterReading | None = None
        for meter in site_data.site.meters:
            for reading in readings.get(meter.service_id, []):
                if reading.meter_stand <= 0:
                    continue
                if best is None or reading.reading_date > best.reading_date:
                    best = reading
        if best:
            site_data.meter_stand = best.meter_stand
            site_data.meter_stand_date = best.reading_date
            site_data.meter_stand_meter_id = best.meter_id

    async def _async_safe_tariffs(self) -> dict[str, Tariff]:
        try:
            return await self.client.async_get_tariffs()
        except CannotConnectError as err:
            _LOGGER.debug("Tariffs unavailable: %s", err)
            return {}

    async def _async_apply_invoiced_prices(
        self,
        model: ConsumptionModel,
        site_data: SiteData,
        invoices: list[Invoice],
        site_count: int,
    ) -> None:
        """Compute the settled SEK/kWh of every invoice: amount / kWh of the month it covers.

        Invoices are issued early in a month for the previous month, so the
        period is the month before the invoice date.  Invoices are matched to
        the site by use place code; with a single site all invoices count.
        """
        matched = [
            inv
            for inv in invoices
            if inv.invoice_date
            and inv.amount > 0
            and (
                site_count == 1
                or site_data.use_place_code is None
                or inv.use_place_code == site_data.use_place_code
            )
        ]
        if not matched:
            return
        periods = [
            (inv, (inv.invoice_date.replace(day=1) - timedelta(days=1)).replace(day=1))
            for inv in matched
            if inv.invoice_date
        ]
        # Monthly series for any year not already loaded (the current year is).
        have_years = {d.year for d in site_data.monthly}
        for year in sorted({p.year for _, p in periods} - have_years, reverse=True):
            previous = await self.client.async_get_consumption(
                model, site_data.site, date(year, 1, 1), date(year, 12, 31), Interval.MONTH
            )
            for point in previous.points:
                key = dt_util.as_local(point.start).date().replace(day=1)
                site_data.monthly.setdefault(key, round(point.value, 3))

        site_data.invoiced = sorted(
            (
                InvoicePrice(
                    period=period,
                    invoice=inv,
                    kwh=(kwh := site_data.monthly.get(period)),
                    price_per_kwh=round(inv.amount / kwh, 4) if kwh else None,
                )
                for inv, period in periods
            ),
            key=lambda ip: (ip.period, ip.invoice.invoice_number),
            reverse=True,
        )

    # --------------------------------------------------------------- statistics
    @staticmethod
    def _statistic_id(site: Site) -> str:
        meter = next((m for m in site.meters if m.is_hourly), None) or (
            site.meters[0] if site.meters else None
        )
        key = meter.meter_id if meter else site.site_id
        return f"{DOMAIN}:{key}_energy"

    async def _async_import_statistics(self, site_data: SiteData) -> None:
        """Push hourly consumption into long-term statistics.

        Rows are keyed by hour start, so re-importing the last days each
        refresh silently corrects values the portal delivered late.
        """
        if not site_data.hourly:
            return
        statistic_id = self._statistic_id(site_data.site)
        # Skip hours the portal has not delivered yet (it reports them as 0).
        cutoff = site_data.last_hour_start
        points = [
            p
            for p in site_data.hourly
            if p.start.minute == 0 and (cutoff is None or p.start <= cutoff)
        ]
        if not points:
            return
        window_start = points[0].start

        recorder = get_instance(self.hass)
        # Running sum just before our window, so the cumulative series continues.
        before = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            window_start - timedelta(hours=1),
            window_start,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        base_sum = 0.0
        if rows := before.get(statistic_id):
            base_sum = float(rows[-1].get("sum") or 0)
        else:
            last = await recorder.async_add_executor_job(
                get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
            )
            if rows := last.get(statistic_id):
                last_start = dt_util.utc_from_timestamp(rows[0]["start"])
                if last_start < window_start:
                    base_sum = float(rows[0].get("sum") or 0)

        running = base_sum
        stats: list[StatisticData] = []
        for point in points:
            running += point.value
            stats.append(StatisticData(start=point.start, state=point.value, sum=round(running, 3)))

        metadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=f"{site_data.site.name} energy",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_of_measurement=site_data.unit,
        )
        async_add_external_statistics(self.hass, metadata, stats)
        self._statistics_imported.add(statistic_id)
        _LOGGER.debug(
            "Imported %d hourly statistics for %s (%s → %s)",
            len(stats),
            statistic_id,
            points[0].start,
            points[-1].start,
        )
