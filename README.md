# Falbygdens Energi for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration) [![Validate](https://github.com/kennedyshead/HA-Falbygdensenergi/actions/workflows/validate.yml/badge.svg)](https://github.com/kennedyshead/HA-Falbygdensenergi/actions/workflows/validate.yml)

Custom integration that logs in to Falbygdens Energi's customer portal
["Mina sidor"](https://minasidor.falbygdensenergi.se/default.aspx) and exposes
the account in Home Assistant.

**Status: working.** Login, hourly/daily/monthly consumption, meter readings,
invoices and long-term statistics for the Energy dashboard are all implemented
and verified against a real account.

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kennedyshead&repository=HA-Falbygdensenergi&category=integration)

Click the badge above, or add the repository by hand:

1. HACS → ⋮ (top right) → *Custom repositories*
2. Repository: `https://github.com/kennedyshead/HA-Falbygdensenergi`, type: *Integration*
3. Search for **Falbygdens Energi**, click *Download*, then restart Home Assistant
4. *Settings → Devices & services → Add integration → Falbygdens Energi*

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=falbygdens_energi)

HACS installs the latest GitHub release and offers updates when a new one is
published.

### Manual

Copy `custom_components/falbygdens_energi` into your Home Assistant
`config/custom_components/` directory and restart.

## Configuration

*Settings → Devices & services → Add integration → Falbygdens Energi*, then
enter the user name and password you use on the portal.

Constraints imposed by the portal:

- **User name + password only.** BankID and Freja logins cannot be automated.
- **No two-factor authentication** on the account. If the portal asks for a
  one-time code the config flow stops with a clear error. Disable 2FA for the
  account, or create a dedicated sub-user (*Administration → Användare*) for
  Home Assistant.
- The portal's login captcha is currently switched off. If Falbygdens Energi
  turns it on, logins will fail with a "captcha" error until they switch it back.

## Entities

One *service* device for the account and one device per use place (address).

**Per use place**

| Entity | Notes |
| --- | --- |
| `sensor.<address>_energy_today` | kWh so far today (`total`, resets at midnight) |
| `sensor.<address>_energy_yesterday` | kWh for yesterday |
| `sensor.<address>_energy_this_month` | Month to date, from the portal's monthly series |
| `sensor.<address>_energy_this_year` | Year to date |
| `sensor.<address>_energy_last_year_same_period` | Same period last year (disabled by default) |
| `sensor.<address>_meter_reading` | Latest meter stand in kWh (`total_increasing`), usually monthly |
| `sensor.<address>_data_up_to` | Start of the last hour the portal has delivered (diagnostic) |

**Account**

| Entity | Notes |
| --- | --- |
| `sensor.<account>_unpaid_invoices` | SEK still to pay, with `unpaid_count` and `overdue_amount` attributes |
| `sensor.<account>_latest_invoice` | Amount of the newest invoice, number and due date as attributes |
| `sensor.<account>_next_due_date` | Earliest due date among unpaid invoices |
| `sensor.<account>_last_update` | Timestamp of the last successful poll (diagnostic) |
| `sensor.<account>_portal_version` | Portal build number, disabled by default (diagnostic) |

### Energy dashboard

Hourly consumption is imported into long-term statistics as
`falbygdens_energi:<meter id>_energy`. In *Settings → Dashboards → Energy →
Grid consumption*, pick that statistic (it is listed under the address name,
not as a sensor). The first refresh backfills 30 days; every later refresh
re-imports the last 3 days so hours the portal delivers late overwrite the
zeros it reports meanwhile.

Polling interval is 6 hours. The portal publishes hourly values with a lag of
a few hours, so faster polling gains little.

## How the portal works

The portal is an instance of CGI's utility customer portal
(`CGI.Utility.Application.CPU.Client.Web`, "pfu3"). Findings so far, all from
the public login page and its JavaScript:

| Item | Detail |
| --- | --- |
| Login | `POST default.aspx/Authenticate` with JSON `{"user","password","captcha":""}` |
| Login reply | `{"d": "<json string>"}` → `{"Result": bool, "LoginResultStatus": int, "Url": "~/start.aspx"}` |
| Status codes | `0` OK, `1` wrong credentials, `2` captcha, `3` 2FA required, `4/5/6` OK variants, `7` locked |
| Session | `.PORTALAUTH` + `ASP.NET_SessionId` cookies, 15 min idle timeout |
| Public settings | `GET api/settings/startupsettings` (captcha flag, login alternatives, landing page) |
| Public version | `GET api/version` |
| Customer context | `<meta name="Portal-CustomerId">`, `Portal-CustomerCode`, `Portal-IsPrivatePerson` on authenticated pages |
| Data pages | `Consumption.aspx`, `Meter.aspx`, `InvoiceList.aspx`, `Contracts.aspx`… return an empty body without login |
| Page methods | Data lives behind ASP.NET page methods (`Page.aspx/Method`, JSON POST, reply wrapped in `d`). Most need the page itself to be GET-ed first in the same session. |
| Sites & meters | `POST Consumption/Consumption.aspx/GetConsumptionViewModelOnLoad` → `SiteGroupNodes[].ConsumptionResolution[]` (meter id, service id, 18-digit facility id) |
| Consumption | `POST Consumption/Consumption.aspx/GetConsumption` with `{"data": "<ConsumptionModel as JSON string>"}`; set `Interval`/`IntervalEnum` (`MONTH`=2, `DAY`=3, `HOUR`=4) and `StartDate`/`EndDate` (`YYYY-MM-DD`). Hourly over 40 days in one call works. |
| Meter stands | `HistoricalMeterReadings.aspx`: `GetUseplaces` → `GetUtilities(useplaceId)` → `GetMeterReadings(serviceId)`, in that order |
| Invoices | `POST Start.aspx/GetInvoices` |

Many API responses are double-encoded (a JSON string that itself contains
JSON). `FalbygdensEnergiClient._unwrap` handles that.

## Extending

`tools/discover.py` logs in, walks the menu and lists every page method each
page uses (output in the git-ignored `tools/out/`). Credentials come from
`.envrc` (`FBE_USERNAME` / `FBE_PASSWORD`, parsed literally so `$` in a
password is safe) or the command line. Note that sourcing `.envrc` in zsh
expands `$` inside double quotes; use single quotes there.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python pytest-homeassistant-custom-component ruff
.venv/bin/ruff check custom_components tests tools
.venv/bin/python -m pytest
```

The tests run a small fake portal (`tests/conftest.py`) that mimics the real
login handshake, so the client is exercised end to end without network access.
