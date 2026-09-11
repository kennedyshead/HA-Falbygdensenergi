"""Sensor platform for Falbygdens Energi."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_CUSTOMER_CODE,
    ATTR_DATA_UP_TO,
    ATTR_DUE_DATE,
    ATTR_FACILITY_ID,
    ATTR_INVOICE_NUMBER,
    ATTR_METER_ID,
    ATTR_OVERDUE_AMOUNT,
    ATTR_READING_DATE,
    ATTR_SERVICE_ID,
    ATTR_SITE_ID,
    ATTR_UNPAID_COUNT,
    BASE_URL,
    DOMAIN,
)
from .coordinator import (
    FalbygdensEnergiConfigEntry,
    FalbygdensEnergiCoordinator,
    HolidayCalendar,
    PortalData,
    SiteData,
    is_high_load_hour,
    next_period_change,
    tariff_schedule,
)

CURRENCY_SEK = "SEK"


@dataclass(frozen=True, kw_only=True)
class AccountSensorDescription(SensorEntityDescription):
    """A sensor derived from account-level data."""

    value_fn: Callable[[PortalData], datetime | date | str | float | None]
    attributes_fn: Callable[[PortalData], dict[str, Any]] | None = None


@dataclass(frozen=True, kw_only=True)
class SiteSensorDescription(SensorEntityDescription):
    """A sensor derived from one site's data."""

    value_fn: Callable[[SiteData], float | datetime | str | None]
    last_reset_fn: Callable[[SiteData], datetime | None] | None = None
    attributes_fn: Callable[[SiteData], dict[str, Any]] | None = None


def _start_of_today(_: SiteData) -> datetime:
    return dt_util.start_of_local_day()


def _start_of_yesterday(_: SiteData) -> datetime:
    return dt_util.start_of_local_day(dt_util.now() - timedelta(days=1))


def _start_of_month(_: SiteData) -> datetime:
    return dt_util.start_of_local_day().replace(day=1)


def _start_of_year(_: SiteData) -> datetime:
    return dt_util.start_of_local_day().replace(month=1, day=1)


def _invoiced_attrs(site: SiteData) -> dict[str, Any]:
    last = site.last_invoiced
    return {
        "period": last.period.strftime("%Y-%m") if last else None,
        ATTR_INVOICE_NUMBER: last.invoice.invoice_number if last else None,
        "invoice_amount": last.invoice.amount if last else None,
        "period_energy": last.kwh if last else None,
        "history": [
            {
                "period": ip.period.strftime("%Y-%m"),
                "energy": ip.kwh,
                "amount": ip.invoice.amount,
                "price": ip.price_per_kwh,
                "invoice_date": ip.invoice.invoice_date.isoformat()
                if ip.invoice.invoice_date
                else None,
            }
            for ip in site.invoiced
        ],
    }


def _month_cost_attrs(site: SiteData) -> dict[str, Any]:
    mc = site.month_cost
    if mc is None:
        return {}
    t = site.tariff
    return {
        "month": mc.month.strftime("%Y-%m"),
        "days_elapsed": mc.days_elapsed,
        "hours_delivered": mc.hours_delivered,
        "energy": mc.kwh,
        "subscription": mc.subscription,
        "transfer": mc.transfer,
        "energy_tax": mc.tax,
        "peak_fee": mc.peak_fee,
        "highload_fee": mc.highload_fee,
        "tariff": t.raw if t else None,
        "tariff_complete": bool(t and t.complete),
    }


def _peak_attrs(site: SiteData) -> dict[str, Any]:
    mc = site.month_cost
    return {"peak_at": mc.peak_at.isoformat() if mc and mc.peak_at else None}


def _highload_attrs(site: SiteData) -> dict[str, Any]:
    mc = site.month_cost
    return {
        "peak_at": mc.highload_at.isoformat() if mc and mc.highload_at else None,
        "in_season": bool(mc and mc.in_highload_season),
        "season": "November–March, weekdays 07:00–19:00 excluding holidays",
    }


def _schedule_attr(
    site: SiteData, day: date, calendar_: HolidayCalendar | None
) -> list[dict[str, Any]]:
    return [
        {
            "start": h.start.isoformat(),
            "end": (h.start + timedelta(hours=1)).isoformat(),
            "period": "high_load" if h.high_load else "normal",
            "energy_price": h.energy_price,
            "peak_fee_per_kw": h.peak_fee_per_kw,
            "highload_fee_per_kw": h.highload_fee_per_kw,
        }
        for h in tariff_schedule(day, site.tariff, calendar_)
    ]


def _tariff_period_attrs(site: SiteData, calendar_: HolidayCalendar | None) -> dict[str, Any]:
    now = dt_util.now()
    today = now.date()
    change = next_period_change(now, calendar_)
    t = site.tariff
    return {
        "high_load_season": now.month in (11, 12, 1, 2, 3),
        "high_load_window": "November–March, Monday–Friday 07:00–19:00, holidays excluded",
        "next_change": change.isoformat() if change else None,
        "energy_price": (
            round(t.transfer_per_kwh + t.tax_per_kwh, 4)
            if t and t.transfer_per_kwh is not None and t.tax_per_kwh is not None
            else None
        ),
        "peak_fee_per_kw": t.peak_per_kw if t else None,
        "highload_fee_per_kw": t.highload_per_kw if t else None,
        "today": _schedule_attr(site, today, calendar_),
        "tomorrow": _schedule_attr(site, today + timedelta(days=1), calendar_),
    }


def _profile_attrs(site: SiteData) -> dict[str, Any]:
    pr = site.profile
    if pr is None:
        return {}
    return {
        "days": pr.days,
        "average": {f"{h:02d}:00": pr.average[h] for h in range(24)},
        "maximum": {f"{h:02d}:00": pr.maximum[h] for h in range(24)},
        "heaviest_hours": [f"{h:02d}:00" for h in pr.heaviest_hours],
        "lightest_hours": [f"{h:02d}:00" for h in pr.lightest_hours],
    }


def _heaviest_hour_label(site: SiteData) -> str | None:
    pr = site.profile
    if pr is None or pr.heaviest_hour is None:
        return None
    return f"{pr.heaviest_hour:02d}:00"


def _start_of_month_cost(_: SiteData) -> datetime:
    return dt_util.start_of_local_day().replace(day=1)


SITE_SENSORS: tuple[SiteSensorDescription, ...] = (
    SiteSensorDescription(
        key="tariff_period",
        translation_key="tariff_period",
        device_class=SensorDeviceClass.ENUM,
        options=["normal", "high_load"],
        icon="mdi:clock-alert-outline",
        value_fn=lambda s: (
            "high_load" if is_high_load_hour(dt_util.now(), s.holidays) else "normal"
        ),
        attributes_fn=lambda s: _tariff_period_attrs(s, s.holidays),
    ),
    SiteSensorDescription(
        key="heaviest_hour",
        translation_key="heaviest_hour",
        icon="mdi:chart-bar",
        value_fn=_heaviest_hour_label,
        attributes_fn=_profile_attrs,
    ),
    SiteSensorDescription(
        key="energy_price_current",
        translation_key="energy_price_current",
        native_unit_of_measurement=f"{CURRENCY_SEK}/{UnitOfEnergy.KILO_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:cash-clock",
        suggested_display_precision=2,
        value_fn=lambda s: s.month_cost.price_per_kwh if s.month_cost else None,
        attributes_fn=_month_cost_attrs,
    ),
    SiteSensorDescription(
        key="grid_cost_month",
        translation_key="grid_cost_month",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY_SEK,
        suggested_display_precision=0,
        value_fn=lambda s: (
            s.month_cost.total if s.month_cost and s.month_cost.price_per_kwh else None
        ),
        last_reset_fn=_start_of_month_cost,
        attributes_fn=_month_cost_attrs,
    ),
    SiteSensorDescription(
        key="grid_cost_projected",
        translation_key="grid_cost_projected",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_SEK,
        suggested_display_precision=0,
        value_fn=lambda s: s.month_cost.projected if s.month_cost else None,
    ),
    SiteSensorDescription(
        key="peak_power_month",
        translation_key="peak_power_month",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value_fn=lambda s: s.month_cost.peak_kw if s.month_cost else None,
        attributes_fn=_peak_attrs,
    ),
    SiteSensorDescription(
        key="highload_peak_month",
        translation_key="highload_peak_month",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value_fn=lambda s: s.month_cost.highload_kw if s.month_cost else None,
        attributes_fn=_highload_attrs,
    ),
    SiteSensorDescription(
        key="energy_price_invoiced",
        translation_key="energy_price_invoiced",
        native_unit_of_measurement=f"{CURRENCY_SEK}/{UnitOfEnergy.KILO_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:cash-multiple",
        suggested_display_precision=2,
        value_fn=lambda s: s.last_invoiced.price_per_kwh if s.last_invoiced else None,
        attributes_fn=_invoiced_attrs,
    ),
    SiteSensorDescription(
        key="energy_today",
        translation_key="energy_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda s: s.today,
        last_reset_fn=_start_of_today,
    ),
    SiteSensorDescription(
        key="energy_yesterday",
        translation_key="energy_yesterday",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda s: s.yesterday,
        last_reset_fn=_start_of_yesterday,
    ),
    SiteSensorDescription(
        key="energy_month",
        translation_key="energy_month",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda s: s.month_to_date,
        last_reset_fn=_start_of_month,
    ),
    SiteSensorDescription(
        key="energy_year",
        translation_key="energy_year",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        value_fn=lambda s: s.year_to_date,
        last_reset_fn=_start_of_year,
    ),
    SiteSensorDescription(
        key="energy_last_year",
        translation_key="energy_last_year",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.last_year_to_date,
    ),
    SiteSensorDescription(
        key="meter_reading",
        translation_key="meter_reading",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        value_fn=lambda s: s.meter_stand,
    ),
    SiteSensorDescription(
        key="data_up_to",
        translation_key="data_up_to",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.last_hour_start,
    ),
)


def _latest_invoice_attrs(data: PortalData) -> dict[str, Any]:
    inv = data.invoices.latest
    if not inv:
        return {}
    return {
        ATTR_INVOICE_NUMBER: inv.invoice_number,
        ATTR_DUE_DATE: inv.due_date.isoformat() if inv.due_date else None,
        "status": inv.status,
        "use_place": inv.use_place,
    }


ACCOUNT_SENSORS: tuple[AccountSensorDescription, ...] = (
    AccountSensorDescription(
        key="unpaid_amount",
        translation_key="unpaid_amount",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_SEK,
        suggested_display_precision=0,
        value_fn=lambda d: d.invoices.unpaid_amount,
        attributes_fn=lambda d: {
            ATTR_UNPAID_COUNT: d.invoices.unpaid_count,
            ATTR_OVERDUE_AMOUNT: d.invoices.overdue_amount,
        },
    ),
    AccountSensorDescription(
        key="latest_invoice",
        translation_key="latest_invoice",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_SEK,
        suggested_display_precision=0,
        value_fn=lambda d: d.invoices.latest.amount if d.invoices.latest else None,
        attributes_fn=_latest_invoice_attrs,
    ),
    AccountSensorDescription(
        key="next_due_date",
        translation_key="next_due_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda d: d.invoices.next_due_date,
    ),
    AccountSensorDescription(
        key="last_update",
        translation_key="last_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.fetched_at,
    ),
    AccountSensorDescription(
        key="portal_version",
        translation_key="portal_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.portal_version,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FalbygdensEnergiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors for a config entry."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        AccountSensor(coordinator, description) for description in ACCOUNT_SENSORS
    ]
    known: set[str] = set()

    def _new_site_entities() -> list[SensorEntity]:
        new: list[SensorEntity] = []
        for site_data in coordinator.data.sites:
            if site_data.site.site_id in known:
                continue
            known.add(site_data.site.site_id)
            new.extend(SiteSensor(coordinator, site_data, d) for d in SITE_SENSORS)
        return new

    entities.extend(_new_site_entities())
    async_add_entities(entities)

    # Sites can appear later (a move, a new contract): add them on refresh.
    entry.async_on_unload(
        coordinator.async_add_listener(
            lambda: (new := _new_site_entities()) and async_add_entities(new)
        )
    )


def _account_device(coordinator: FalbygdensEnergiCoordinator) -> DeviceInfo:
    entry = coordinator.config_entry
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        entry_type=DeviceEntryType.SERVICE,
        manufacturer="Falbygdens Energi",
        model="Mina sidor",
        name=entry.title,
        configuration_url=BASE_URL,
        sw_version=(
            str(coordinator.client.info.portal_version)
            if coordinator.client.info.portal_version
            else None
        ),
    )


class AccountSensor(CoordinatorEntity[FalbygdensEnergiCoordinator], SensorEntity):
    """A value about the account as a whole."""

    _attr_has_entity_name = True
    entity_description: AccountSensorDescription

    def __init__(
        self, coordinator: FalbygdensEnergiCoordinator, description: AccountSensorDescription
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"
        self._attr_device_info = _account_device(coordinator)

    @property
    def native_value(self) -> datetime | date | str | float | None:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the customer code plus any description-specific attributes."""
        attrs: dict[str, Any] = {ATTR_CUSTOMER_CODE: self.coordinator.client.info.customer_code}
        if self.entity_description.attributes_fn:
            attrs.update(self.entity_description.attributes_fn(self.coordinator.data))
        return attrs


class SiteSensor(CoordinatorEntity[FalbygdensEnergiCoordinator], SensorEntity):
    """A value for one site (use place / metering point)."""

    _attr_has_entity_name = True
    entity_description: SiteSensorDescription

    def __init__(
        self,
        coordinator: FalbygdensEnergiCoordinator,
        site_data: SiteData,
        description: SiteSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._site_id = site_data.site.site_id
        entry = coordinator.config_entry
        meter = site_data.primary_meter
        self._attr_unique_id = f"{entry.entry_id}_site_{self._site_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_site_{self._site_id}")},
            via_device=(DOMAIN, entry.entry_id),
            manufacturer="Falbygdens Energi",
            model="Elnät" if (meter and meter.utility_id == "E") else "Mätpunkt",
            name=site_data.site.name,
            serial_number=meter.meter_id if meter else None,
            configuration_url=f"{BASE_URL}/consumption/consumption.aspx",
        )

    def _site(self) -> SiteData | None:
        return next(
            (s for s in self.coordinator.data.sites if s.site.site_id == self._site_id), None
        )

    @property
    def available(self) -> bool:
        """Only available while the site is still on the account."""
        return super().available and self._site() is not None

    @property
    def native_value(self) -> float | datetime | str | None:
        """Return the sensor value."""
        site = self._site()
        return self.entity_description.value_fn(site) if site else None

    @property
    def last_reset(self) -> datetime | None:
        """Period start for the TOTAL sensors."""
        site = self._site()
        if site and self.entity_description.last_reset_fn:
            return self.entity_description.last_reset_fn(site)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose identifiers and freshness."""
        site = self._site()
        if not site:
            return {}
        meter = site.primary_meter
        attrs: dict[str, Any] = {
            ATTR_SITE_ID: site.site.site_id,
            ATTR_METER_ID: meter.meter_id if meter else None,
            ATTR_SERVICE_ID: meter.service_id if meter else None,
            ATTR_FACILITY_ID: meter.service_identifier if meter else None,
            ATTR_DATA_UP_TO: site.last_hour_start.isoformat() if site.last_hour_start else None,
        }
        if self.entity_description.key == "meter_reading":
            attrs[ATTR_READING_DATE] = (
                site.meter_stand_date.isoformat() if site.meter_stand_date else None
            )
            attrs[ATTR_METER_ID] = site.meter_stand_meter_id or attrs[ATTR_METER_ID]
        if self.entity_description.attributes_fn:
            attrs.update(self.entity_description.attributes_fn(site))
        return attrs
