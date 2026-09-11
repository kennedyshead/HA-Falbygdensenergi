"""Constants for the Falbygdens Energi integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "falbygdens_energi"

BASE_URL = "https://minasidor.falbygdensenergi.se"

CONF_CUSTOMER_ID = "customer_id"
CONF_CUSTOMER_CODE = "customer_code"

# The portal only publishes hourly meter values once per day (typically the
# following morning), so polling more often than this just burns sessions.
DEFAULT_SCAN_INTERVAL = timedelta(hours=6)

# Portal session idle timeout is 15 minutes (see sitemaster js); we never keep
# a session that long, but re-login is transparent anyway.
SESSION_TIMEOUT = timedelta(minutes=14)

ATTR_CUSTOMER_CODE = "customer_code"
ATTR_METER_ID = "meter_id"
ATTR_FACILITY_ID = "facility_id"
ATTR_LAST_READING = "last_reading"

# How far back hourly values are fetched the first time a meter is seen
# (one request; the portal handled 40 days / 960 points fine).
STATISTICS_BACKFILL_DAYS = 30
# Re-imported into statistics every refresh so late-delivered hours overwrite
# earlier zeros.
STATISTICS_REFRESH_DAYS = 3
# Hourly history always fetched, for the hour-of-day consumption profile.
PROFILE_DAYS = 30

ATTR_SITE_ID = "site_id"
ATTR_SERVICE_ID = "service_id"
ATTR_READING_DATE = "reading_date"
ATTR_DATA_UP_TO = "data_up_to"
ATTR_INVOICE_NUMBER = "invoice_number"
ATTR_DUE_DATE = "due_date"
ATTR_UNPAID_COUNT = "unpaid_count"
ATTR_OVERDUE_AMOUNT = "overdue_amount"
