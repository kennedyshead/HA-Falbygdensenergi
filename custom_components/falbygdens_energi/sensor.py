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
from homeassistant.const import EntityCategory, UnitOfEnergy
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
    PortalData,
    SiteData,
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

    value_fn: Callable[[SiteData], float | datetime | None]
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


def _price_attrs(site: SiteData) -> dict[str, Any]:
    inv = site.price_invoice
    return {
        ATTR_INVOICE_NUMBER: inv.invoice_number if inv else None,
        "invoice_amount": inv.amount if inv else None,
        "invoice_date": inv.invoice_date.isoformat() if inv and inv.invoice_date else None,
        "period": site.price_period.strftime("%Y-%m") if site.price_period else None,
        "period_energy": site.price_kwh,
    }


SITE_SENSORS: tuple[SiteSensorDescription, ...] = (
    SiteSensorDescription(
        key="energy_price",
        translation_key="energy_price",
        native_unit_of_measurement=f"{CURRENCY_SEK}/{UnitOfEnergy.KILO_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:cash-multiple",
        suggested_display_precision=2,
        value_fn=lambda s: s.price_per_kwh,
        attributes_fn=_price_attrs,
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
        sw_version=coordinator.client.info.portal_version,
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
    def native_value(self) -> float | datetime | None:
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
