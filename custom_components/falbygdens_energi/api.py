"""Async client for the Falbygdens Energi "Mina sidor" customer portal.

The portal is an instance of CGI's utility customer portal (ASP.NET WebForms
front-end with a JSON API underneath).  Login is a JSON "page method" on
``default.aspx``; the server answers with an ``.PORTALAUTH`` cookie that
authorises every later ``api/...`` call.  Sessions idle out after 15 minutes,
so the client re-authenticates transparently when the portal starts refusing
requests.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import IntEnum
from typing import Any

import aiohttp
from yarl import URL

from .const import BASE_URL, SESSION_TIMEOUT

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)

_META_RE = re.compile(r'<meta\s+name="Portal-([A-Za-z]+)"\s+content="([^"]*)"', re.I)
_INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
_ENDPOINT_RE = re.compile(
    r"""[`'"]([^`'"\s]*?(?:api/|\.aspx/)[A-Za-z0-9_/${}.\-]+)""",
)
_NAV_LINK_RE = re.compile(r'href="([^"#]+\.aspx)"', re.I)
_SKIP_LINKS = ("http", "logout", "default", "mailto")


class LoginResultStatus(IntEnum):
    """``LoginResultStatus`` values returned by ``default.aspx/Authenticate``."""

    OK = 0
    NOT_OK = 1
    CAPTCHA_NOT_OK = 2
    TWO_FACTOR_NOT_OK = 3
    EMT_ONLY_OK = 4
    EMT_ADMIN_OK = 5
    PFU_ONLY_OK = 6
    LOCKED_ACCOUNT = 7


_SUCCESS_STATUSES = {
    LoginResultStatus.OK,
    LoginResultStatus.EMT_ONLY_OK,
    LoginResultStatus.EMT_ADMIN_OK,
    LoginResultStatus.PFU_ONLY_OK,
}


class FalbygdensEnergiError(Exception):
    """Base error for the portal client."""


class CannotConnectError(FalbygdensEnergiError):
    """The portal could not be reached or answered unexpectedly."""


class AuthenticationError(FalbygdensEnergiError):
    """Wrong user name or password."""


class AccountLockedError(AuthenticationError):
    """The portal reports the account as locked."""


class TwoFactorRequiredError(AuthenticationError):
    """The account has two-factor authentication enabled.

    The portal asks for a one-time code after the password step.  A headless
    integration cannot complete that interactively, so the user must disable
    2FA for the account used by Home Assistant (or create a dedicated sub-user
    without it).
    """


class CaptchaRequiredError(AuthenticationError):
    """The portal has turned on its captcha, which we cannot solve."""


class PasswordLoginDisabledError(AuthenticationError):
    """The portal only allows BankID / Freja login."""


@dataclass(slots=True)
class PortalInfo:
    """Facts about the portal instance and the logged-in customer."""

    customer_id: str | None = None
    customer_code: str | None = None
    is_private_person: bool | None = None
    portal_version: str | None = None
    landing_url: str | None = None
    raw_meta: dict[str, str] = field(default_factory=dict)


class Interval(IntEnum):
    """Resolution values understood by ``Consumption.aspx/GetConsumption``."""

    MONTH = 2
    DAY = 3
    HOUR = 4


_INTERVAL_NAMES = {Interval.MONTH: "MONTH", Interval.DAY: "DAY", Interval.HOUR: "HOUR"}

_MS_DATE_RE = re.compile(r"/Date\((-?\d+)\)/")


def parse_ms_date(value: Any) -> datetime | None:
    """Parse the ``/Date(1788904800000)/`` timestamps (ms since epoch, UTC)."""
    if not isinstance(value, str):
        return None
    if m := _MS_DATE_RE.fullmatch(value.strip()):
        ms = int(m.group(1))
        if ms < 0:  # DateTime.MinValue, used as "not set"
            return None
        return datetime.fromtimestamp(ms / 1000, tz=UTC)
    try:
        return datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None


@dataclass(slots=True)
class MeterInfo:
    """One metering service (meter) attached to a site."""

    meter_id: str
    service_id: str
    service_identifier: str  # 18-digit facility id (anläggnings-ID)
    utility_id: str  # "E" electricity, "F" district heating, "V" water …
    unit_type_id: str | None = None
    is_hourly: bool = True
    from_date: datetime | None = None
    to_date: datetime | None = None


@dataclass(slots=True)
class Site:
    """A use place (address) with its meters, as listed on the consumption page."""

    site_id: str
    name: str
    utility_ids: list[str] = field(default_factory=list)
    meters: list[MeterInfo] = field(default_factory=list)


@dataclass(slots=True)
class ConsumptionPoint:
    """Consumption for one interval starting at ``start``."""

    start: datetime
    value: float
    unit: str = "kWh"


@dataclass(slots=True)
class ConsumptionResult:
    """Parsed ``GetConsumption`` response for one site."""

    interval: Interval
    unit: str
    points: list[ConsumptionPoint]
    current_period_total: float | None = None
    last_year_period_total: float | None = None

    @property
    def total(self) -> float:
        """Sum of all points."""
        return round(sum(p.value for p in self.points), 3)


@dataclass(slots=True)
class MeterReading:
    """A historical meter reading (meter stand)."""

    service_id: str
    meter_id: str
    reading_date: datetime
    meter_stand: float
    consumption: float | None
    status: str | None
    reason: str | None
    updated: datetime | None


@dataclass(slots=True)
class Invoice:
    """An invoice as listed on the start page."""

    invoice_id: str
    invoice_number: str
    invoice_date: date | None
    due_date: date | None
    amount: float
    remaining_amount: float
    status: str | None
    overdue: bool
    use_place: str | None
    use_place_code: str | None
    business_area: str | None
    total_balance: float | None
    total_overdue_amount: float | None


@dataclass(slots=True)
class Tariff:
    """Grid tariff for one contract, in SEK (VAT included for private customers).

    Falbygdens Energi's Normaltaxa has five components; the invoice computes
    subscription × days/365, energy × (transfer + tax), highest hour × peak
    fee and, November–March, highest weekday 07–19 hour × high-load fee.
    """

    subscription_per_year: float | None = None
    transfer_per_kwh: float | None = None
    tax_per_kwh: float | None = None
    peak_per_kw: float | None = None
    highload_per_kw: float | None = None
    contract_name: str | None = None
    includes_vat: bool = True
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """True when everything needed to price a month is known."""
        return None not in (
            self.subscription_per_year,
            self.transfer_per_kwh,
            self.tax_per_kwh,
            self.peak_per_kw,
        )


_PRICE_RE = re.compile(r"(-?\d[\d\s]*(?:[.,]\d+)?)\s*(kr|öre|sek)\s*/\s*(år|mån|kwh|kw)", re.I)

# Portal price labels -> Tariff fields.
_PRICE_FIELDS = {
    "abonnemang": "subscription_per_year",
    "elöverföring": "transfer_per_kwh",
    "överföring": "transfer_per_kwh",
    "effektavgift": "peak_per_kw",
    "höglastavgift": "highload_per_kw",
    "energiskatt": "tax_per_kwh",
}


def parse_price(text: str) -> tuple[float, str] | None:
    """Parse ``"37,20 öre/kWh"`` → ``(0.372, "kwh")`` (value in SEK per unit).

    ``kr/mån`` is converted to SEK per year so subscriptions compare directly.
    """
    if not (m := _PRICE_RE.search(text or "")):
        return None
    number = float(m.group(1).replace(" ", "").replace(",", "."))
    if m.group(2).lower() == "öre":
        number /= 100
    unit = m.group(3).lower()
    if unit == "mån":
        return round(number * 12, 6), "år"
    return round(number, 6), unit


def parse_tariff(prices: list[dict[str, Any]], *, include_vat: bool = True) -> Tariff:
    """Build a Tariff from the ``Prices`` list of a contract."""
    tariff = Tariff(includes_vat=include_vat)
    key = "PriceVat" if include_vat else "PriceNoVat"
    for row in prices or []:
        label = str(row.get("PriceLabel") or "").strip()
        text = str(row.get(key) or "")
        tariff.raw[label] = text
        field_name = _PRICE_FIELDS.get(label.lower())
        if not field_name or not (parsed := parse_price(text)):
            continue
        setattr(tariff, field_name, parsed[0])
    return tariff


@dataclass(slots=True)
class ConsumptionModel:
    """The server's view model for the consumption page plus the sites it lists."""

    raw_model: dict[str, Any]
    sites: list[Site]
    available_units: list[str]
    show_hourly_cost: bool


class FalbygdensEnergiClient:
    """Thin async wrapper around the portal's HTTP surface."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        base_url: str = BASE_URL,
    ) -> None:
        """Initialise the client.  ``session`` must have a cookie jar."""
        self._session = session
        self._username = username
        self._password = password
        self._base = URL(base_url.rstrip("/") + "/")
        self._login_lock = asyncio.Lock()
        self._logged_in_at: datetime | None = None
        self.info = PortalInfo()
        # site id -> use place code (the code invoices refer to); filled by
        # async_get_meter_readings, which is the only place the portal lists it.
        self.use_place_codes: dict[str, str] = {}

    # ------------------------------------------------------------------ helpers
    def _url(self, path: str) -> URL:
        return self._base.join(URL(path.lstrip("/")))

    @property
    def is_logged_in(self) -> bool:
        """Return True if we have a session that is unlikely to have expired."""
        return (
            self._logged_in_at is not None and datetime.now() - self._logged_in_at < SESSION_TIMEOUT
        )

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        """Unwrap the portal's double-encoded JSON.

        The API frequently returns a JSON *string* whose content is JSON, and
        ASP.NET page methods wrap the result in ``{"d": ...}``.
        """
        if isinstance(payload, dict) and set(payload) == {"d"}:
            payload = payload["d"]
        for _ in range(2):
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    break
        return payload

    # -------------------------------------------------------------------- login
    async def async_get_startup_settings(self) -> dict[str, str]:
        """Return the public start-up settings as a flat ``name -> value`` dict."""
        try:
            async with self._session.get(
                self._url("api/settings/startupsettings"), timeout=DEFAULT_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                data = self._unwrap(await resp.json(content_type=None))
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnectError(f"Cannot fetch start-up settings: {err}") from err
        if not isinstance(data, list):
            raise CannotConnectError("Unexpected start-up settings payload")
        return {item.get("Name", ""): str(item.get("Value", "")) for item in data}

    async def async_login(self) -> PortalInfo:
        """Authenticate with user name and password.

        Raises a subclass of :class:`AuthenticationError` when the portal
        rejects the credentials or requires an interactive step.
        """
        async with self._login_lock:
            self._logged_in_at = None
            settings = await self.async_get_startup_settings()
            if settings.get("DisableUserPassLogin", "False").lower() == "true":
                raise PasswordLoginDisabledError("Portal only allows BankID/Freja login")
            if settings.get("IsCaptchaFeatureActive", "False").lower() == "true":
                raise CaptchaRequiredError("Portal has enabled captcha on login")

            try:
                # Priming GET: sets ASP.NET_SessionId and pfu_lang cookies.
                async with self._session.get(
                    self._url("default.aspx"), timeout=DEFAULT_TIMEOUT
                ) as resp:
                    resp.raise_for_status()
                async with self._session.post(
                    self._url("default.aspx/Authenticate"),
                    json={"user": self._username, "password": self._password, "captcha": ""},
                    headers={
                        "Accept": "application/json",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=DEFAULT_TIMEOUT,
                ) as resp:
                    resp.raise_for_status()
                    result = self._unwrap(await resp.json(content_type=None))
            except (aiohttp.ClientError, TimeoutError) as err:
                raise CannotConnectError(f"Login request failed: {err}") from err

            if not isinstance(result, dict) or "LoginResultStatus" not in result:
                raise CannotConnectError(f"Unexpected login response: {result!r}")

            try:
                status = LoginResultStatus(int(result["LoginResultStatus"]))
            except ValueError as err:
                raise CannotConnectError(
                    f"Unknown LoginResultStatus {result['LoginResultStatus']!r}"
                ) from err

            if status is LoginResultStatus.TWO_FACTOR_NOT_OK:
                raise TwoFactorRequiredError("Account requires a two-factor code")
            if status is LoginResultStatus.LOCKED_ACCOUNT:
                raise AccountLockedError("Account is locked")
            if status is LoginResultStatus.CAPTCHA_NOT_OK:
                raise CaptchaRequiredError("Portal rejected the (empty) captcha")
            if status not in _SUCCESS_STATUSES or not result.get("Result"):
                raise AuthenticationError("Wrong user name or password")

            self._logged_in_at = datetime.now()
            landing = str(result.get("Url") or "start.aspx").lstrip("~/")
            self.info.landing_url = str(self._url(landing))
            _LOGGER.debug("Logged in to portal, status=%s landing=%s", status.name, landing)

            await self._async_load_portal_info()
            return self.info

    async def _async_load_portal_info(self) -> None:
        """Read the ``Portal-*`` meta tags from the landing page."""
        assert self.info.landing_url is not None
        html = await self.async_get_page(self.info.landing_url)
        meta = {name: value for name, value in _META_RE.findall(html)}
        self.info.raw_meta = meta
        self.info.customer_id = meta.get("CustomerId") or None
        self.info.customer_code = meta.get("CustomerCode") or None
        if "IsPrivatePerson" in meta:
            self.info.is_private_person = meta["IsPrivatePerson"].lower() == "true"
        if version := meta.get("Version"):
            if m := re.search(r"\[([\d.]+)\]", version):
                self.info.portal_version = m.group(1)

    async def async_ensure_login(self) -> None:
        """Log in unless a fresh session already exists."""
        if not self.is_logged_in:
            await self.async_login()

    # ----------------------------------------------------------------- requests
    async def async_request(
        self,
        method: str,
        path: str,
        *,
        retry: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Perform an authenticated API request and return decoded JSON.

        A 401, or an HTML login page in place of JSON, triggers one re-login
        and retry.
        """
        await self.async_ensure_login()
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        headers.update(kwargs.pop("headers", {}))
        try:
            async with self._session.request(
                method, self._url(path), headers=headers, **kwargs
            ) as resp:
                if resp.status in (401, 403) and retry:
                    _LOGGER.debug("Portal returned %s for %s, re-authenticating", resp.status, path)
                    self._logged_in_at = None
                    return await self.async_request(method, path, retry=False, **kwargs)
                resp.raise_for_status()
                text = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnectError(f"{method} {path} failed: {err}") from err

        if text.lstrip().startswith("<") and retry:
            # Got a page (probably the login page) instead of JSON: session died.
            self._logged_in_at = None
            return await self.async_request(method, path, retry=False, **kwargs)
        try:
            return self._unwrap(json.loads(text)) if text else None
        except ValueError as err:
            raise CannotConnectError(f"{method} {path} returned non-JSON: {text[:120]!r}") from err

    async def async_get_page(self, url_or_path: str) -> str:
        """Fetch an authenticated HTML page and return its body."""
        await self.async_ensure_login()
        url = URL(url_or_path) if "://" in url_or_path else self._url(url_or_path)
        try:
            async with self._session.get(url, timeout=DEFAULT_TIMEOUT) as resp:
                resp.raise_for_status()
                self._logged_in_at = datetime.now()
                return await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnectError(f"GET {url} failed: {err}") from err

    # -------------------------------------------------------------- public API
    async def async_get_version(self) -> dict[str, Any]:
        """Return ``api/version`` (public, also useful as a connectivity check)."""
        return await self.async_request("GET", "api/version")

    async def async_has_subsidiaries(self) -> bool:
        """Return whether the customer has sub-customers to choose between."""
        return bool(self._unwrap(await self.async_request("GET", "api/customer/hassubsidiaries")))

    # -------------------------------------------------------------- page methods
    async def async_page_method(self, page: str, method: str, **payload: Any) -> Any:
        """Call an ASP.NET page method (``Page.aspx/Method``) and unwrap ``d``.

        Page methods are plain JSON POSTs.  Several of them rely on server-side
        session state that only exists after the page itself was requested, so
        callers load the page first (see ``async_load_consumption_model``).
        """
        return await self.async_request("POST", f"{page}/{method}", json=payload)

    async def async_load_consumption_model(self) -> ConsumptionModel:
        """Load the consumption page and return its view model with all sites.

        The GET on the page seeds the server session with the customer's
        default site; ``GetConsumptionViewModelOnLoad`` then returns the model
        that the browser posts back (modified) to fetch data.
        """
        await self.async_get_page("consumption/consumption.aspx")
        vm = await self.async_page_method(
            "Consumption/Consumption.aspx", "GetConsumptionViewModelOnLoad"
        )
        if not isinstance(vm, dict) or not isinstance(vm.get("ConsumptionModel"), dict):
            raise CannotConnectError(f"Unexpected consumption view model: {str(vm)[:200]}")

        sites: list[Site] = []
        for node in vm.get("SiteGroupNodes") or []:
            site = Site(
                site_id=str(node.get("SiteGroupId")),
                name=str(node.get("SiteGroupName") or node.get("SiteGroupId")),
                utility_ids=[
                    str(u.get("UtilityId"))
                    for u in node.get("Utilities") or []
                    if u.get("UtilityId")
                ],
            )
            for res in node.get("ConsumptionResolution") or []:
                site.meters.append(
                    MeterInfo(
                        meter_id=str(res.get("MeterId") or res.get("ServiceId")),
                        service_id=str(res.get("ServiceId")),
                        service_identifier=str(res.get("ServiceIdentifier") or ""),
                        utility_id=str(res.get("UtilityId") or "E"),
                        unit_type_id=res.get("UnitTypeId"),
                        is_hourly=str(res.get("IsHourly")) in ("4", "True", "true", "1"),
                        from_date=parse_ms_date(res.get("MeasurandFromDate")),
                        to_date=parse_ms_date(res.get("MeasurandToDate")),
                    )
                )
            sites.append(site)

        units = [str(u.get("UnitId")) for u in vm.get("AvailableUnits") or [] if u.get("UnitId")]
        model = vm["ConsumptionModel"]
        return ConsumptionModel(
            raw_model=model,
            sites=sites,
            available_units=units,
            show_hourly_cost=str(model.get("ShowHourlyCost", "")).upper() == "Y",
        )

    async def async_get_consumption(
        self,
        model: ConsumptionModel,
        site: Site,
        start: date,
        end: date,
        interval: Interval,
        unit: str = "kWh",
        utility_id: str | None = None,
    ) -> ConsumptionResult:
        """Fetch consumption for ``site`` between ``start`` and ``end`` (inclusive).

        Hourly queries may span many days; 40 days (960 points) in one call
        has been verified to work.
        """
        payload = copy.deepcopy(model.raw_model)
        payload.update(
            {
                "SiteId": site.site_id,
                "SiteName": site.name,
                "UtilityId": utility_id or payload.get("UtilityId") or "E",
                "Interval": _INTERVAL_NAMES[interval],
                "IntervalEnum": int(interval),
                "StartDate": start.isoformat(),
                "EndDate": end.isoformat(),
                "TargetUnit": unit,
                "CompareType": "none",
                "IsPageLoad": False,
                "IsCompareConsumption": False,
            }
        )
        # The browser sends the model as a JSON *string* in a "data" property.
        result = await self.async_page_method(
            "Consumption/Consumption.aspx", "GetConsumption", data=json.dumps(payload)
        )
        if not isinstance(result, dict):
            raise CannotConnectError(f"Unexpected GetConsumption response: {str(result)[:200]}")

        points: list[ConsumptionPoint] = []
        result_unit = unit
        chart = result.get("DetailedConsumptionChart") or {}
        for series in chart.get("SeriesList") or []:
            # Only the consumption series for the requested utility; the portal
            # can add temperature / cost series depending on settings.
            if str(series.get("id")) != (utility_id or payload["UtilityId"]):
                continue
            for point in series.get("data") or []:
                start_dt = parse_ms_date(point.get("date"))
                value = point.get("y")
                if start_dt is None or value is None:
                    continue
                result_unit = point.get("seriesUnit") or result_unit
                points.append(
                    ConsumptionPoint(start=start_dt, value=float(value), unit=result_unit)
                )
            break
        points.sort(key=lambda p: p.start)

        compare = result.get("CompareModel") or {}
        return ConsumptionResult(
            interval=interval,
            unit=result_unit,
            points=points,
            current_period_total=_as_float(compare.get("CurrYearValue")),
            last_year_period_total=_as_float(compare.get("LastYearValue")),
        )

    async def async_get_meter_readings(self) -> dict[str, list[MeterReading]]:
        """Return historical meter readings keyed by service id.

        Mirrors the page's call order, which the server requires:
        page → GetUseplaces → GetUtilities(useplace) → GetMeterReadings(service).
        """
        page = "Consumption/HistoricalMeterReadings.aspx"
        await self.async_get_page("consumption/historicalmeterreadings.aspx")
        useplaces = await self.async_page_method(page, "GetUseplaces") or []
        readings: dict[str, list[MeterReading]] = {}
        for useplace in useplaces:
            useplace_id = str(useplace.get("UsePlaceId"))
            if code := useplace.get("UsePlaceCode"):
                self.use_place_codes[useplace_id] = str(code)
            try:
                utilities = await self.async_page_method(
                    page, "GetUtilities", useplaceId=useplace_id
                )
            except CannotConnectError as err:
                _LOGGER.debug("GetUtilities failed for use place %s: %s", useplace_id, err)
                continue
            for contract in (utilities or {}).get("ContractTypes") or []:
                service_id = str(contract.get("ServiceId"))
                try:
                    rows = await self.async_page_method(
                        page, "GetMeterReadings", serviceId=service_id
                    )
                except CannotConnectError as err:
                    _LOGGER.debug("GetMeterReadings failed for service %s: %s", service_id, err)
                    continue
                parsed: list[MeterReading] = []
                for row in rows or []:
                    reading_date = parse_ms_date(row.get("readingDate"))
                    if reading_date is None:
                        continue
                    parsed.append(
                        MeterReading(
                            service_id=service_id,
                            meter_id=str(row.get("meterId") or ""),
                            reading_date=reading_date,
                            meter_stand=float(row.get("meterStand") or 0),
                            consumption=_as_float(row.get("consumption")),
                            status=row.get("translatedReadingStatus") or row.get("readingStatus"),
                            reason=row.get("translatedReadingReason"),
                            updated=parse_ms_date(row.get("updatedDate")),
                        )
                    )
                parsed.sort(key=lambda r: (r.reading_date, r.meter_stand))
                readings[service_id] = parsed
        return readings

    async def async_get_invoices(self) -> list[Invoice]:
        """Return the invoices listed on the start page (most recent first)."""
        rows = await self.async_page_method("Start.aspx", "GetInvoices") or []
        invoices: list[Invoice] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            invoices.append(
                Invoice(
                    invoice_id=str(row.get("InvoiceId")),
                    invoice_number=str(row.get("InvoiceNumber") or ""),
                    invoice_date=_parse_iso_date(row.get("InvoiceDate")),
                    due_date=_parse_iso_date(row.get("PresentDueDate")),
                    amount=float(row.get("Amount") or 0),
                    remaining_amount=float(row.get("RemainingAmount") or 0),
                    status=row.get("StatusFilter"),
                    overdue=bool(row.get("InvoiceOverDue")),
                    use_place=row.get("UsePlace"),
                    use_place_code=row.get("UseplaceCode"),
                    business_area=row.get("BusinessArea"),
                    total_balance=_as_float(row.get("TotalBalance")),
                    total_overdue_amount=_as_float(row.get("TotalOverDueAmount")),
                )
            )
        invoices.sort(key=lambda i: (i.invoice_date or date.min, i.invoice_number), reverse=True)
        return invoices

    async def async_get_tariffs(self) -> dict[str, Tariff]:
        """Return the electricity grid tariff per site id from the contracts page.

        Call order mirrors the page: page → GetLocalSettings → GetUseplaces →
        GetContractDetails(usePlaces) → GetContractsAddtionalInformation for
        each contract, which is where the price list lives.
        """
        page = "Contract/Contracts.aspx"
        await self.async_get_page("contract/contracts.aspx")
        settings = await self.async_page_method(page, "GetLocalSettings") or {}
        include_vat = not bool(settings.get("IsLegalEntity"))
        useplaces = await self.async_page_method(page, "GetUseplaces") or []
        ids: list[str] = []
        for up in useplaces:
            ids.extend(str(up.get("UseplaceIds") or "").split(","))
        ids = [i.strip() for i in ids if i.strip()]
        if not ids:
            return {}
        contracts = await self.async_page_method(page, "GetContractDetails", usePlaces=ids) or []

        tariffs: dict[str, Tariff] = {}
        for contract in contracts:
            if str(contract.get("UtilityId")) != "E" or str(contract.get("StatusId")) != "1":
                continue
            site_id = str(contract.get("UsePlaceId"))
            try:
                # The server wants the contract object as a JSON *string*.
                details = await self.async_page_method(
                    page, "GetContractsAddtionalInformation", selectedcontract=json.dumps(contract)
                )
            except CannotConnectError as err:
                _LOGGER.debug("Contract details unavailable for %s: %s", site_id, err)
                continue
            tariff = parse_tariff((details or {}).get("Prices") or [], include_vat=include_vat)
            tariff.contract_name = contract.get("UtilityName")
            tariffs[site_id] = tariff
        return tariffs

    # ---------------------------------------------------------------- discovery
    async def async_discover(self) -> dict[str, Any]:
        """Crawl the authenticated pages and list every API endpoint they use.

        The data pages (consumption, meters, invoices) are only served after
        login, and each embeds its own JavaScript with the endpoints it calls.
        This helper is what ``tools/discover.py`` uses to map the API so the
        consumption fetchers can be written against real responses.
        """
        await self.async_ensure_login()
        assert self.info.landing_url is not None
        landing = await self.async_get_page(self.info.landing_url)
        pages: dict[str, dict[str, Any]] = {}
        queue = ["start.aspx"] + sorted(
            {
                link
                for link in _NAV_LINK_RE.findall(landing)
                if not link.lower().startswith(_SKIP_LINKS)
            }
        )
        seen: set[str] = set()
        while queue:
            page = queue.pop(0).lstrip("~/")
            if page.lower() in seen:
                continue
            seen.add(page.lower())
            try:
                html = await self.async_get_page(page)
            except CannotConnectError as err:
                pages[page] = {"error": str(err)}
                continue
            scripts = "\n".join(_INLINE_SCRIPT_RE.findall(html))
            endpoints = sorted(
                {e for e in _ENDPOINT_RE.findall(scripts) if "api/" in e or ".aspx/" in e}
            )
            pages[page] = {
                "size": len(html),
                "endpoints": endpoints,
                "script_chars": len(scripts),
            }
            for link in _NAV_LINK_RE.findall(html):
                if link.lower() not in seen and not link.lower().startswith(
                    ("http", "logout", "default", "mailto")
                ):
                    queue.append(link)
        return {"info": self.info.raw_meta, "pages": pages}


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_portal_datetime(value: str) -> datetime:
    """Parse the ``2026-09-11T10:46:51`` / ``2026-09-11 10:46:51`` timestamps the portal emits."""
    return datetime.fromisoformat(value.replace(" ", "T"))


__all__ = [
    "AccountLockedError",
    "AuthenticationError",
    "CannotConnectError",
    "CaptchaRequiredError",
    "ConsumptionModel",
    "ConsumptionPoint",
    "ConsumptionResult",
    "FalbygdensEnergiClient",
    "FalbygdensEnergiError",
    "Interval",
    "Invoice",
    "LoginResultStatus",
    "MeterInfo",
    "MeterReading",
    "PasswordLoginDisabledError",
    "PortalInfo",
    "Site",
    "Tariff",
    "TwoFactorRequiredError",
    "parse_ms_date",
    "parse_portal_datetime",
    "parse_price",
    "parse_tariff",
]
