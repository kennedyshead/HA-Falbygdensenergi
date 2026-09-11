"""Shared fixtures."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Let Home Assistant load custom_components/ in tests.

    The integration depends on ``recorder``, whose test database must be
    prepared *before* ``hass`` is created, so pull ``recorder_mock`` in first
    for every test that uses ``hass``.
    """
    if "hass" in request.fixturenames:
        _patch_recorder_annotations()
        request.getfixturevalue("recorder_mock")
        request.getfixturevalue("enable_custom_integrations")


def _patch_recorder_annotations() -> None:
    """Work around a Python 3.14 + pytest-homeassistant-custom-component clash.

    The plugin autospecs several recorder functions; on Python 3.14 that
    evaluates their (lazy) annotations, and names such as ``Recorder`` and
    ``Session`` are only imported under ``TYPE_CHECKING`` or inside functions
    in those modules.  Making the names resolvable is harmless.
    """
    import importlib  # noqa: PLC0415

    from homeassistant.components import recorder  # noqa: PLC0415
    from sqlalchemy.orm.session import Session  # noqa: PLC0415

    for mod_name in (
        "homeassistant.components.recorder.migration",
        "homeassistant.components.recorder.util",
        "homeassistant.components.recorder.statistics",
        "homeassistant.components.recorder.core",
        "homeassistant.helpers.recorder",
        "pytest_homeassistant_custom_component.plugins",
        "pytest_homeassistant_custom_component.patch_recorder",
        "pytest_homeassistant_custom_component.components.recorder.common",
    ):
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        mod.__dict__.setdefault("Recorder", recorder.Recorder)
        mod.__dict__.setdefault("Session", Session)


STARTUP_SETTINGS = [
    {"Name": "IsCaptchaFeatureActive", "Value": "False"},
    {"Name": "DisableUserPassLogin", "Value": "False"},
    {"Name": "DefaultLandingPage", "Value": "~/start.aspx"},
]


def ms(dt: datetime) -> str:
    """Format a datetime the way ASP.NET's JSON serializer does."""
    return f"/Date({int(dt.timestamp() * 1000)})/"


# Fixed "now" used by the fake portal: 2026-09-11 10:00 UTC.
FAKE_NOW = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)
SITE_ID = "1133402154"
SERVICE_ID = "1133448153"
METER_ID = "55782955"

CONSUMPTION_MODEL = {
    "UtilityId": "E",
    "SiteId": SITE_ID,
    "SiteName": "Testgatan 1, TESTSTAD",
    "Interval": "DAY",
    "IntervalEnum": 3,
    "StartDate": ms(FAKE_NOW - timedelta(days=40)),
    "EndDate": ms(FAKE_NOW),
    "TargetUnit": "kWh",
    "CustomerId": 12345,
    "ShowHourlyCost": "Y",
}

ONLOAD_RESPONSE = {
    "__type": "CGI.Utility.Application.CPU.Client.Web.ViewModels.ConsumptionViewModel",
    "ConsumptionModel": CONSUMPTION_MODEL,
    "SiteGroupNodes": [
        {
            "SiteGroupId": int(SITE_ID),
            "SiteGroupName": "Testgatan 1, TESTSTAD",
            "SiteGroupType": "Z",
            "ConsumptionResolution": [
                {
                    "IsHourly": "4",
                    "MeterId": METER_ID,
                    "ServiceId": SERVICE_ID,
                    "ServiceIdentifier": "735999151202487543",
                    "UnitTypeId": "101",
                    "UtilityId": "E",
                    "MeasurandFromDate": ms(FAKE_NOW - timedelta(days=400)),
                }
            ],
            "Utilities": [{"UtilityId": "E", "Key": "Electricity"}],
        }
    ],
    "AvailableUnits": [{"UtilityId": "E", "UnitId": "kWh"}, {"UtilityId": "E", "UnitId": "MWh"}],
}

CONTRACT = {
    "ContractId": "1133966412",
    "UsePlaceId": SITE_ID,
    "StatusId": "1",
    "UtilityName": "Elnät - Nätavtal",
    "UtilityId": "E",
    "ServiceId": SERVICE_ID,
    "Prices": None,
}

TARIFF_PRICES = [
    {"PriceLabel": "Abonnemang", "PriceVat": "4525,00 kr/år", "PriceNoVat": "3620,00 kr/år"},
    {"PriceLabel": "Elöverföring", "PriceVat": "37,20 öre/kWh", "PriceNoVat": "29,76 öre/kWh"},
    {"PriceLabel": "Effektavgift", "PriceVat": "45,00 kr/kW", "PriceNoVat": "36,00 kr/kW"},
    {"PriceLabel": "Höglastavgift", "PriceVat": "65,00 kr/kW", "PriceNoVat": "52,00 kr/kW"},
    {"PriceLabel": "Energiskatt", "PriceVat": "45,00 öre/kWh", "PriceNoVat": "36,00 öre/kWh"},
    {"PriceLabel": "Beräknad årskostnad", "PriceVat": "0,00 kr", "PriceNoVat": "0,00 kr"},
]

LANDING_HTML = """<html><head>
<meta name="Portal-Version" content="CPU.Client.Web.dll [13.0.26138.52557] [IsDebug=False]" />
<meta name="Portal-CustomerId" content="12345" />
<meta name="Portal-CustomerCode" content="ABC123" />
<meta name="Portal-IsPrivatePerson" content="true" />
</head><body id="bodyNode" class="page-overview">
<a class="nav-link" href="Consumption.aspx">Förbrukning</a>
<script>
  $.ajax({ url: siteRoot + 'api/consumption/meters' });
</script>
</body></html>"""


class FakePortal:
    """A tiny stand-in for the real portal, driven by the test."""

    def __init__(self) -> None:
        self.valid = {"user": "secret"}
        self.login_status: int | None = None  # force a status regardless of creds
        self.captcha_active = False
        self.authenticated = False
        self.calls: list[str] = []
        self.consumption_requests: list[dict] = []
        self.latest_invoice_date = "2026-09-04"
        self.previous_invoice_date = "2026-08-10"

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/default.aspx", self.default)
        app.router.add_post("/default.aspx/Authenticate", self.authenticate)
        app.router.add_get("/api/settings/startupsettings", self.startup)
        app.router.add_get("/api/version", self.version)
        app.router.add_get("/start.aspx", self.landing)
        app.router.add_get("/Consumption.aspx", self.landing)
        app.router.add_get("/api/consumption/meters", self.meters)
        app.router.add_get("/consumption/consumption.aspx", self.landing)
        app.router.add_get("/consumption/historicalmeterreadings.aspx", self.landing)
        app.router.add_post(
            "/Consumption/Consumption.aspx/GetConsumptionViewModelOnLoad", self.onload
        )
        app.router.add_post("/Consumption/Consumption.aspx/GetConsumption", self.consumption)
        app.router.add_post(
            "/Consumption/HistoricalMeterReadings.aspx/GetUseplaces", self.useplaces
        )
        app.router.add_post(
            "/Consumption/HistoricalMeterReadings.aspx/GetUtilities", self.utilities
        )
        app.router.add_post(
            "/Consumption/HistoricalMeterReadings.aspx/GetMeterReadings", self.meter_readings
        )
        app.router.add_post("/Start.aspx/GetInvoices", self.invoices)
        app.router.add_get("/contract/contracts.aspx", self.landing)
        app.router.add_post("/Contract/Contracts.aspx/GetLocalSettings", self.local_settings)
        app.router.add_post("/Contract/Contracts.aspx/GetUseplaces", self.contract_useplaces)
        app.router.add_post("/Contract/Contracts.aspx/GetContractDetails", self.contract_details)
        app.router.add_post(
            "/Contract/Contracts.aspx/GetContractsAddtionalInformation", self.contract_info
        )
        return app

    # -------------------------------------------------------------- contracts
    async def local_settings(self, request: web.Request) -> web.Response:
        if denied := self._check(request):
            return denied
        return web.json_response({"d": {"IsPrivatePerson": True, "IsLegalEntity": False}})

    async def contract_useplaces(self, request: web.Request) -> web.Response:
        if denied := self._check(request):
            return denied
        return web.json_response(
            {"d": [{"FullAddress": "Testgatan 1, TESTSTAD", "UseplaceIds": SITE_ID}]}
        )

    async def contract_details(self, request: web.Request) -> web.Response:
        if denied := self._check(request):
            return denied
        body = await request.json()
        assert body == {"usePlaces": [SITE_ID]}
        return web.json_response({"d": [CONTRACT]})

    async def contract_info(self, request: web.Request) -> web.Response:
        if denied := self._check(request):
            return denied
        body = await request.json()
        assert json.loads(body["selectedcontract"]) == CONTRACT  # sent as a JSON string
        return web.json_response({"d": {**CONTRACT, "Prices": TARIFF_PRICES}})

    # ------------------------------------------------------------ data pages
    def _hourly_value(self, start: datetime) -> float:
        """Deterministic hourly kWh: 1.0 + hour/100; 0 for hours not delivered yet."""
        if start >= FAKE_NOW - timedelta(hours=2):
            return 0.0
        return round(1.0 + start.hour / 100, 3)

    async def onload(self, request: web.Request) -> web.Response:
        if denied := self._check(request):
            return denied
        self.calls.append("onload")
        return web.json_response({"d": ONLOAD_RESPONSE})

    async def consumption(self, request: web.Request) -> web.Response:
        if denied := self._check(request):
            return denied
        body = await request.json()
        model = json.loads(body["data"])  # the browser double-encodes the model
        self.calls.append(f"consumption:{model['Interval']}")
        self.consumption_requests.append(model)
        start = datetime.fromisoformat(model["StartDate"]).replace(tzinfo=UTC)
        end = datetime.fromisoformat(model["EndDate"]).replace(tzinfo=UTC)
        points = []
        if model["IntervalEnum"] == 4:  # HOUR
            cur = start
            while cur < end + timedelta(days=1):
                points.append({"y": self._hourly_value(cur), "date": ms(cur), "seriesUnit": "kWh"})
                cur += timedelta(hours=1)
        elif model["IntervalEnum"] == 2:  # MONTH
            for month in range(1, 13):
                first = datetime(start.year, month, 1, tzinfo=UTC)
                if first > FAKE_NOW:
                    break
                points.append({"y": 1000.0 + month, "date": ms(first), "seriesUnit": "kWh"})
        else:  # DAY
            cur = start
            while cur <= end:
                points.append({"y": 24.0, "date": ms(cur), "seriesUnit": "kWh"})
                cur += timedelta(days=1)
        return web.json_response(
            {
                "d": {
                    "ConsumptionModel": model,
                    "DetailedConsumptionChart": {
                        "SeriesList": [
                            {
                                "id": "T",
                                "name": "Temperature",
                                "data": [{"y": 12, "date": ms(start)}],
                            },
                            {"id": "E", "name": "El kWh", "data": points},
                        ]
                    },
                    "CompareModel": {"CurrYearValue": 9009.0, "LastYearValue": 12000.0},
                }
            }
        )

    async def useplaces(self, request: web.Request) -> web.Response:
        if denied := self._check(request):
            return denied
        return web.json_response({"d": [{"UsePlaceId": int(SITE_ID), "UsePlaceCode": "53015634"}]})

    async def utilities(self, request: web.Request) -> web.Response:
        if denied := self._check(request):
            return denied
        body = await request.json()
        assert body == {"useplaceId": SITE_ID}
        return web.json_response(
            {"d": {"ContractTypes": [{"UtilityName": "El", "ServiceId": SERVICE_ID}]}}
        )

    async def meter_readings(self, request: web.Request) -> web.Response:
        if denied := self._check(request):
            return denied
        body = await request.json()
        assert body == {"serviceId": SERVICE_ID}
        day = datetime(2026, 9, 1, tzinfo=UTC)
        rows = [
            {
                "serviceID": int(SERVICE_ID),
                "readingDate": ms(day),
                "meterStand": 0,
                "consumption": 0,
                "meterId": METER_ID,
                "translatedReadingStatus": "Godkänd",
            },
            {
                "serviceID": int(SERVICE_ID),
                "readingDate": ms(day),
                "meterStand": 116041.594,
                "consumption": 983.001,
                "meterId": METER_ID,
                "translatedReadingStatus": "Godkänd",
                "translatedReadingReason": "Normal",
                "updatedDate": ms(day + timedelta(days=3)),
            },
            {
                "serviceID": int(SERVICE_ID),
                "readingDate": ms(day - timedelta(days=31)),
                "meterStand": 115058.593,
                "consumption": 900.0,
                "meterId": METER_ID,
            },
        ]
        return web.json_response({"d": rows})

    async def invoices(self, request: web.Request) -> web.Response:
        if denied := self._check(request):
            return denied
        rows = [
            {
                "InvoiceId": 1,
                "InvoiceNumber": "100",
                "InvoiceDate": self.previous_invoice_date,
                "PresentDueDate": "2026-08-31",
                "Amount": 1938.0,
                "RemainingAmount": 0.0,
                "StatusFilter": "Betald",
                "InvoiceOverDue": False,
                "UseplaceCode": "53015634",
            },
            {
                "InvoiceId": 2,
                "InvoiceNumber": "101",
                "InvoiceDate": self.latest_invoice_date,
                "PresentDueDate": "2026-09-30",
                "Amount": 1790.0,
                "RemainingAmount": 1790.0,
                "StatusFilter": "Obetald",
                "InvoiceOverDue": False,
                "UseplaceCode": "53015634",
                "TotalBalance": 1790.0,
            },
        ]
        return web.json_response({"d": rows})

    async def default(self, request: web.Request) -> web.Response:
        self.calls.append("default")
        resp = web.Response(text="<html></html>", content_type="text/html")
        resp.set_cookie("ASP.NET_SessionId", "abc")
        return resp

    async def startup(self, request: web.Request) -> web.Response:
        settings = [dict(s) for s in STARTUP_SETTINGS]
        if self.captcha_active:
            settings[0]["Value"] = "True"
        # The real portal double-encodes: a JSON string containing JSON.
        return web.json_response(json.dumps(settings))

    async def authenticate(self, request: web.Request) -> web.Response:
        self.calls.append("authenticate")
        body = await request.json()
        if self.login_status is not None:
            status = self.login_status
        elif self.valid.get(body.get("user")) == body.get("password"):
            status = 0
        else:
            status = 1
        ok = status in (0, 4, 5, 6)
        self.authenticated = ok
        inner = {"Result": ok, "LoginResultStatus": status, "Url": "~/start.aspx" if ok else ""}
        resp = web.json_response({"d": json.dumps(inner)})
        if ok:
            resp.set_cookie(".PORTALAUTH", "token")
        return resp

    def _check(self, request: web.Request) -> web.Response | None:
        if request.cookies.get(".PORTALAUTH") != "token" or not self.authenticated:
            return web.json_response(
                {"Message": "Authorization has been denied for this request."}, status=401
            )
        return None

    async def version(self, request: web.Request) -> web.Response:
        self.calls.append("version")
        return web.json_response(
            {"FileVersionServer": "13.0.26138.52557", "Time": "2026-09-11 10:46:51"}
        )

    async def landing(self, request: web.Request) -> web.Response:
        if denied := self._check(request):
            return denied
        return web.Response(text=LANDING_HTML, content_type="text/html")

    async def meters(self, request: web.Request) -> web.Response:
        if denied := self._check(request):
            return denied
        return web.json_response([{"MeterId": "1"}])


@pytest.fixture
async def portal(socket_enabled: None) -> AsyncIterator[tuple[FakePortal, str]]:
    """Run the fake portal on a local port and yield (portal, base_url).

    The Home Assistant test plugin blocks sockets by default; ``socket_enabled``
    (from pytest-socket) lifts that for tests using this fixture.
    """
    fake = FakePortal()
    server = TestServer(fake.app())
    await server.start_server(access_log=None)
    try:
        yield fake, str(server.make_url("/"))
    finally:
        await server.close()
