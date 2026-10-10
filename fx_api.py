"""Shared core for the Visa, Mastercard, and UnionPay currency calculators.

This module fetches public currency-conversion rates from each network,
caches results in a local SQLite database, and logs every HTTP interaction.
"""

import datetime as dt
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from typing import Any

from curl_cffi import requests

# Logging setup.
logger = logging.getLogger("bestrates")
if not logger.handlers:
  _log_handler = logging.StreamHandler()
  _log_formatter = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] [bestrates] %(message)s",
    datefmt="%H:%M:%S",
  )
  _log_handler.setFormatter(_log_formatter)
  logger.addHandler(_log_handler)


DB_PATH = "fx_db.sqlite"
_INIT_LOCK = threading.Lock()
_initialized_db_path: str | None = None


FRANKFURTER_URL = "https://api.frankfurter.dev/v2"
VISA_URL = "https://www.visa.co.in/cmsapi/fx/rates"
MASTERCARD_URL = (
  "https://www.mastercard.com/marketingservices/public/mccom-services/"
  "currency-conversions/conversion-rates"
)
UNIONPAY_URL_TEMPLATE = "https://www.unionpayintl.com/upload/jfimg/{date}.json"

_DATE_FMT = "%Y-%m-%d"


FRANKFURTER_HEADERS = {"Accept": "application/json"}
VISA_HEADERS = {
  "Accept": "application/json, text/plain, */*",
  "Referer": (
    "https://www.visa.co.in/support/consumer/travel-support/"
    "exchange-rate-calculator.html"
  ),
}
MASTERCARD_HEADERS = {
  "Accept": "*/*",
  "Referer": (
    "https://www.mastercard.com/in/en/personal/get-support/"
    "currency-exchange-rate-converter.html"
  ),
}
UNIONPAY_HEADERS = {
  "Accept": "application/json, text/plain, */*",
  "Referer": "https://www.unionpayintl.com/en/rate/",
}

# ---------------- Database Helpers ----------------
def _get_connection() -> sqlite3.Connection:
  """Returns a SQLite connection with row factory configured."""
  dir_name = os.path.dirname(os.path.abspath(DB_PATH))
  if dir_name and not os.path.exists(dir_name):
    os.makedirs(dir_name, exist_ok=True)
  conn = sqlite3.connect(DB_PATH, timeout=30.0)
  conn.row_factory = sqlite3.Row
  # Set SQLite's internal busy timeout (ms) so the engine backs off before
  # raising OperationalError in WAL mode under concurrent access.
  conn.execute("PRAGMA busy_timeout = 30000")
  return conn


def _init_db() -> None:
  """Initializes the SQLite database schema if not already present."""
  global _initialized_db_path
  if _initialized_db_path == DB_PATH and os.path.exists(DB_PATH):
    return
  with _INIT_LOCK:
    if _initialized_db_path == DB_PATH and os.path.exists(DB_PATH):
      return
    with _get_connection() as conn:
      conn.execute("PRAGMA journal_mode=WAL")
      conn.execute("PRAGMA synchronous=NORMAL")

      # 1. Network rates table
      conn.execute(
        """
        CREATE TABLE IF NOT EXISTS network_rates (
          network TEXT NOT NULL,
          date TEXT NOT NULL,
          home TEXT NOT NULL,
          spend TEXT NOT NULL,
          rate REAL,
          network_benchmark REAL,
          saved_at TEXT,
          raw_data TEXT,
          PRIMARY KEY (network, date, home, spend)
        )
        """
      )
      conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_network_rates_date ON network_rates (date)"
      )

      # 2. Benchmark rates table (Frankfurter, etc.)
      conn.execute(
        """
        CREATE TABLE IF NOT EXISTS benchmark_rates (
          source TEXT NOT NULL,
          date TEXT NOT NULL,
          base TEXT NOT NULL,
          quote TEXT NOT NULL,
          rate REAL NOT NULL,
          saved_at TEXT,
          raw_data TEXT,
          PRIMARY KEY (source, date, base, quote)
        )
        """
      )
      conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_benchmark_rates_lookup ON benchmark_rates (date, base, quote)"
      )

      conn.commit()
    _initialized_db_path = DB_PATH


def _row_to_cached_value(row: sqlite3.Row) -> dict[str, Any]:
  """Converts a network_rates DB row to a cached-value dict.

  Attempts to deserialize ``raw_data`` as JSON first; falls back to
  building a minimal dict from the individual scalar columns.
  """
  if row["raw_data"]:
    try:
      return json.loads(row["raw_data"])
    except (json.JSONDecodeError, TypeError):
      pass
  result: dict[str, Any] = {}
  if row["rate"] is not None:
    result["rate"] = row["rate"]
  if row["network_benchmark"] is not None:
    result["network_benchmark"] = row["network_benchmark"]
  if row["saved_at"] is not None:
    result["_saved_at"] = row["saved_at"]
  return result


def db_get(
  network: str,
  date_str: str,
  home: str,
  spend: str,
) -> dict[str, Any] | None:
  """Retrieves an entry from the network_rates table."""
  _init_db()
  try:
    with _get_connection() as conn:
      cursor = conn.execute(
        """
        SELECT rate, network_benchmark, saved_at, raw_data
        FROM network_rates
        WHERE network = ? AND date = ? AND home = ? AND spend = ?
        """,
        (
          network.lower(),
          date_str,
          home.upper(),
          spend.upper(),
        ),
      )
      row = cursor.fetchone()
      if not row:
        return None

      cached_value = _row_to_cached_value(row)

      logger.debug(
        "[CACHE HIT] %s | %s | %s/%s",
        network.upper(),
        date_str,
        home,
        spend,
      )
      return cached_value
  except sqlite3.Error as error:
    logger.warning("Database error during get: %s", error)
    return None


def db_get_network_rates_range(
  network: str,
  home: str,
  spend: str,
  dates: list[dt.date],
) -> dict[str, dict[str, Any]]:
  """Retrieves cached network_rates rows for specific dates in one query.

  Replaces the common pattern of calling ``db_get`` once per date inside a
  loop. Requested dates are fetched with one ``SELECT … IN (…)`` statement.

  Args:
    network: Network identifier (e.g. ``'visa'``).
    home: Cardholder billing currency code.
    spend: Transaction currency code.
    dates: Dates to include in the lookup.

  Returns:
    A mapping of ``date_str -> cached record dict``.  Missing dates are
    simply absent from the mapping; the caller should treat absence the
    same as a cache miss (i.e. fall back to a network request).
  """
  _init_db()
  date_strs = sorted({date.strftime(_DATE_FMT) for date in dates})
  if not date_strs:
    return {}
  placeholders = ",".join("?" for _ in date_strs)

  result: dict[str, dict[str, Any]] = {}
  try:
    with _get_connection() as conn:
      cursor = conn.execute(
        """
        SELECT date, rate, network_benchmark, saved_at, raw_data
        FROM network_rates
        WHERE network = ? AND home = ? AND spend = ?
          AND date IN (""" + placeholders + """)
        ORDER BY date
        """,
        [network.lower(), home.upper(), spend.upper(), *date_strs],
      )
      for row in cursor.fetchall():
        cached_value = _row_to_cached_value(row)

        result[row["date"]] = cached_value

  except sqlite3.Error as error:
    logger.warning("Database error during range get: %s", error)

  logger.debug(
    "[CACHE DATES] %s | %s/%s | %d dates → %d rows",
    network.upper(),
    home,
    spend,
    len(date_strs),
    len(result),
  )
  return result


def db_set(
  network: str,
  date_str: str,
  home: str,
  spend: str,
  record: dict[str, Any],
) -> None:
  """Stores an entry in the network_rates table."""
  _init_db()
  record_with_meta = dict(record)
  saved_at = (
    record_with_meta.get("_saved_at")
    or dt.datetime.now(dt.timezone.utc).isoformat()
  )
  record_with_meta["_saved_at"] = saved_at

  rate = record_with_meta.get("rate")
  network_benchmark = record_with_meta.get(
    "network_benchmark"
  ) or record_with_meta.get("benchmark")
  raw_data = json.dumps(record_with_meta, ensure_ascii=False)

  try:
    with _get_connection() as conn:
      conn.execute(
        """
        INSERT INTO network_rates (
          network, date, home, spend,
          rate, network_benchmark, saved_at, raw_data
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(network, date, home, spend) DO UPDATE SET
          rate = excluded.rate,
          network_benchmark = excluded.network_benchmark,
          saved_at = excluded.saved_at,
          raw_data = excluded.raw_data
        """,
        (
          network.lower(),
          date_str,
          home.upper(),
          spend.upper(),
          rate,
          network_benchmark,
          saved_at,
          raw_data,
        ),
      )
      conn.commit()
  except sqlite3.Error as error:
    logger.warning("Database error during set: %s", error)


def db_get_benchmark(
  source: str,
  date_str: str,
  base: str,
  quote: str,
) -> float | None:
  """Retrieves an entry from the benchmark_rates table.

  Args:
    source: Benchmark source name (e.g. 'frankfurter' or 'visa').
    date_str: Date string YYYY-MM-DD.
    base: The base (spend) currency code.
    quote: The quote (home/billing) currency code.

  Returns:
    The benchmark rate as a float, or None.
  """
  _init_db()
  try:
    with _get_connection() as conn:
      cursor = conn.execute(
        """
        SELECT rate FROM benchmark_rates
        WHERE source = ? AND date = ? AND base = ? AND quote = ?
        """,
        (source.lower(), date_str, base.upper(), quote.upper()),
      )
      row = cursor.fetchone()
      if row and row["rate"] is not None:
        return row["rate"]
      return None
  except sqlite3.Error as error:
    logger.warning("Database error during benchmark get: %s", error)
    return None


def db_has_benchmark_rates(
  source: str,
  dates: list[dt.date],
  home: str,
  spend_currencies: Iterable[str] | None = None,
) -> bool:
  """Checks whether the database contains cached benchmark rates for the dates.

  If ``spend_currencies`` is provided, checks that every requested currency has at
  least one cached rate in the date range. Otherwise, checks that at least one rate
  exists for the given quote/home currency in the range.

  Args:
    source: Benchmark source name (e.g. 'frankfurter').
    dates: Dates to check.
    home: Cardholder billing / quote currency code.
    spend_currencies: Optional iterable of spend currency codes to check.

  Returns:
    True if the required benchmark records exist in the database, False otherwise.
  """
  _init_db()
  date_strs = sorted({date.strftime(_DATE_FMT) for date in dates})
  if not date_strs:
    return False
  date_placeholders = ",".join("?" for _ in date_strs)

  home_code = home.upper()
  try:
    with _get_connection() as conn:
      if spend_currencies:
        spend_list = [
          s.upper() for s in spend_currencies if s.upper() != home_code
        ]
        if not spend_list:
          return True

        placeholders = ",".join("?" for _ in spend_list)
        query = f"""
          SELECT COUNT(DISTINCT base) FROM benchmark_rates
          WHERE source = ?
            AND quote = ?
            AND date IN ({date_placeholders})
            AND base IN ({placeholders})
        """
        cursor = conn.execute(
          query,
          [source.lower(), home_code, *date_strs]
          + spend_list,
        )
        row = cursor.fetchone()
        distinct_bases = row[0] if row else 0
        return distinct_bases >= len(set(spend_list))
      else:
        query = f"""
          SELECT 1 FROM benchmark_rates
          WHERE source = ? AND quote = ?
            AND date IN ({date_placeholders})
          LIMIT 1
          """
        cursor = conn.execute(
          query,
          [source.lower(), home_code, *date_strs],
        )
        return cursor.fetchone() is not None
  except sqlite3.Error as error:
    logger.warning("Database error during benchmark range check: %s", error)
    return False


def db_set_benchmark(
  source: str,
  date_str: str,
  base: str,
  quote: str,
  rate: float,
  raw_data: Any | None = None,
) -> None:
  """Stores an entry in the benchmark_rates table."""
  db_set_benchmarks_batch([(source, date_str, base, quote, rate, raw_data)])


def db_set_benchmarks_batch(
  records: Iterable[tuple[str, str, str, str, float, Any | None]],
) -> None:
  """Stores multiple entries in the benchmark_rates table in a single transaction.

  Args:
    records: An iterable of tuples: ``(source, date_str, base, quote, rate, raw_data)``
  """
  if not records:
    return
  _init_db()
  saved_at = dt.datetime.now(dt.timezone.utc).isoformat()
  rows = []
  for item in records:
    source, date_str, base, quote, rate, raw_data = item
    raw_str = (
      json.dumps(raw_data, ensure_ascii=False)
      if (raw_data and not isinstance(raw_data, str))
      else raw_data
    )
    rows.append(
      (
        source.lower(),
        str(date_str),
        base.upper(),
        quote.upper(),
        rate,
        saved_at,
        raw_str,
      )
    )

  try:
    with _get_connection() as conn:
      conn.executemany(
        """
        INSERT INTO benchmark_rates (source, date, base, quote, rate, saved_at, raw_data)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, date, base, quote) DO UPDATE SET
          rate = excluded.rate,
          saved_at = excluded.saved_at,
          raw_data = excluded.raw_data
        """,
        rows,
      )
      conn.commit()
  except sqlite3.Error as error:
    logger.warning(
      "Database error during benchmark batch set: %s", error
    )


# ---------------- HTTP Helpers ----------------
def session() -> requests.Session:
  """Creates an HTTP session that impersonates Chrome.

  Returns:
    A ``curl_cffi`` session object.
  """
  return requests.Session(impersonate="chrome")


def _to_float(value: Any) -> float | None:
  """Converts a value to a float without raising an exception.

  Args:
    value: The value to convert.

  Returns:
    The value as a float, or None if conversion fails.
  """
  try:
    return float(value)
  except (TypeError, ValueError):
    return None


def _get(
  http_session: requests.Session,
  url: str,
  headers: dict[str, str],
  params: dict[str, Any] | None = None,
  tries: int = 4,
) -> Any | None:
  """Performs a GET request with logging and retries.

  Args:
    http_session: The HTTP session used to send the request.
    url: The URL to request.
    headers: A dictionary of HTTP headers.
    params: An optional dictionary of query parameters.
    tries: The maximum number of attempts.

  Returns:
    The decoded JSON response body, or None if the resource is not
    found or every attempt fails.
  """
  param_str = f" with params={params}" if params else ""
  for attempt_index in range(tries):
    attempt_str = (
      f" (attempt {attempt_index + 1}/{tries})"
      if attempt_index > 0
      else ""
    )
    logger.info(
      "[HTTP GET] Initiating request to %s%s%s",
      url,
      param_str,
      attempt_str,
    )
    start_time = time.time()

    try:
      response = http_session.get(
        url, headers=headers, params=params, timeout=30
      )
      elapsed = time.time() - start_time

      logger.info(
        "[HTTP %s] Completed in %.2fs from %s",
        response.status_code,
        elapsed,
        url,
      )

      if response.status_code == 200:
        return response.json()
      if response.status_code in (400, 404):
        logger.warning(
          "[HTTP %s] Resource not found or not yet published at %s",
          response.status_code,
          url,
        )
        return None

      logger.warning(
        "[HTTP %s] Unexpected status response from %s",
        response.status_code,
        url,
      )

    except requests.exceptions.RequestException as request_error:
      elapsed = time.time() - start_time
      logger.warning(
        "[HTTP ERROR] Network exception after %.2fs: %s",
        elapsed,
        request_error,
      )
    except json.JSONDecodeError as decode_error:
      logger.error(
        "[JSON ERROR] Failed to decode JSON from %s: %s",
        url,
        decode_error,
      )
    except Exception as unexpected_error:  # pylint: disable=broad-except
      logger.error("[UNEXPECTED ERROR] %s", unexpected_error)

    # Back off before the next attempt.
    sleep_seconds = 1.2 * (attempt_index + 1)
    if attempt_index < tries - 1:
      logger.info("Retrying in %.1fs...", sleep_seconds)
      time.sleep(sleep_seconds)

  logger.error("[HTTP FAILED] Exhausted %d attempts for %s", tries, url)
  return None


def _cached_range_hits(
  network: str,
  dates: list[dt.date],
  home: str,
  spend: str,
  amount: float,
) -> dict[str, dict[str, Any]]:
  """Returns valid cached results for a date batch with one database query."""
  if not dates:
    return {}

  cached_rows = db_get_network_rates_range(
    network,
    home,
    spend,
    dates,
  )
  results: dict[str, dict[str, Any]] = {}
  for date_str, cached in cached_rows.items():
    if not cached.get("rate"):
      continue
    result: dict[str, Any] = {
      "rate": cached["rate"],
      "bill": round(cached["rate"] * amount, 4),
      "cached": True,
    }
    if cached.get("network_benchmark") is not None:
      result["network_benchmark"] = cached["network_benchmark"]
    results[date_str] = result
  return results


# ---------------- Benchmark Rates ----------------
def prefetch_benchmark_rates(
  http_session: requests.Session,
  dates: list[dt.date],
  home: str,
  spend_currencies: Iterable[str] | None = None,
  force: bool = False,
) -> int:
  """Prefetches Frankfurter benchmark rates for a date range in a single batch API call.

  Checks the database first and skips the network request if cached benchmark rates
  already exist for the date range and requested currencies (unless ``force=True``).
  Stores the rates directly into the benchmark_rates table in SQLite so subsequent
  calls to ``get_benchmark_rate`` will hit the local cache without network requests.

  Args:
    http_session: The HTTP session used to send the request.
    dates: Dates to fetch and cache.
    home: The cardholder billing / quote currency code (e.g. 'SGD').
    spend_currencies: Optional list/set of spend currency codes to restrict to.
    force: If True, bypass the database cache check and re-fetch from the API.

  Returns:
    Number of rate records saved to the database (or 0 if cached/skipped).
  """
  date_strs = sorted({date.strftime(_DATE_FMT) for date in dates})
  if not date_strs:
    return 0
  start_str = date_strs[0]
  end_str = date_strs[-1]

  home_code = home.upper()

  if not force and db_has_benchmark_rates(
    "frankfurter", dates, home_code, spend_currencies
  ):
    logger.info(
      "Benchmark rates for %s (%s to %s) already cached in database. Skipping API prefetch.",
      home_code,
      start_str,
      end_str,
    )
    return 0

  url = f"{FRANKFURTER_URL}/rates"
  params = {
    "from": start_str,
    "to": end_str,
    "base": home_code,
  }

  spend_filter = None
  if spend_currencies:
    spend_filter = {s.upper() for s in spend_currencies}
    spend_filter.discard(home_code)

  logger.info(
    "Prefetching Frankfurter benchmark rates from %s to %s (base %s)...",
    start_str,
    end_str,
    home_code,
  )
  json_body = _get(http_session, url, FRANKFURTER_HEADERS, params)
  if not json_body or not isinstance(json_body, list):
    logger.warning("Failed to prefetch Frankfurter benchmark rates.")
    return 0

  records_to_save = []
  for item in json_body:
    if item.get("date") not in date_strs:
      continue
    quote_curr = item.get("quote", "").upper()
    if not quote_curr or quote_curr == home_code:
      continue
    if spend_filter and quote_curr not in spend_filter:
      continue

    raw_rate = _to_float(item.get("rate"))
    if not raw_rate or raw_rate <= 0:
      continue

    # In Frankfurter /rates with base=home:
    # rate is quote units per 1 home unit (e.g. USD per 1 SGD).
    # We need spend units as base and home as quote:
    # 1 spend = (1 / raw_rate) home units.
    calc_rate = round(1.0 / raw_rate, 6)
    item_date = item.get("date")
    if item_date:
      records_to_save.append(
        (
          "frankfurter",
          item_date,
          quote_curr,
          home_code,
          calc_rate,
          item,
        )
      )

  if records_to_save:
    db_set_benchmarks_batch(records_to_save)
    logger.info(
      "Prefetched and cached %d Frankfurter benchmark records.",
      len(records_to_save),
    )

  return len(records_to_save)


def get_benchmark_rate(
  http_session: requests.Session,
  date: dt.date,
  home: str,
  spend: str,
) -> tuple[float | None, str | None]:
  """Gets the benchmark reference rate, defaulting to Frankfurter (ECB).

  If Frankfurter does not have a rate for the pair/date, falls back to
  Visa's ECB benchmark rate if available.

  Args:
    http_session: The HTTP session used to send the request.
    date: The date for which to look up the rate.
    home: The cardholder billing / quote currency code.
    spend: The transaction / base currency code.

  Returns:
    A tuple of ``(rate, source)`` where rate is home per spend unit,
    and source is ``'frankfurter'``, ``'visa'``, or ``(None, None)``.
  """
  date_str = date.strftime(_DATE_FMT)
  home_code = home.upper()
  spend_code = spend.upper()

  # 1. Check cached Frankfurter benchmark
  cached_ff = db_get_benchmark("frankfurter", date_str, spend_code, home_code)
  if cached_ff is not None:
    return cached_ff, "frankfurter"

  # 2. Fetch from Frankfurter API
  json_body = _get(
    http_session,
    f"{FRANKFURTER_URL}/rate/{spend_code}/{home_code}",
    FRANKFURTER_HEADERS,
    {"date": date_str},
  )
  if json_body:
    rate = _to_float(json_body.get("rate"))
    if rate is not None:
      db_set_benchmark(
        "frankfurter", date_str, spend_code, home_code, rate, json_body
      )
      logger.info(
        "Using Frankfurter benchmark for %s/%s on %s: %s",
        home_code,
        spend_code,
        date_str,
        rate,
      )
      return rate, "frankfurter"

  # 3. Fallback: check cached Visa benchmark
  cached_visa = db_get_benchmark("visa", date_str, spend_code, home_code)
  if cached_visa is not None:
    return cached_visa, "visa"

  # Also check network_rates for visa's network_benchmark
  visa_net = db_get("visa", date_str, home_code, spend_code)
  if visa_net and visa_net.get("network_benchmark") is not None:
    v_bm = visa_net["network_benchmark"]
    db_set_benchmark("visa", date_str, spend_code, home_code, v_bm)
    return v_bm, "visa"

  logger.warning(
    "No benchmark rate available for %s/%s on %s",
    home_code,
    spend_code,
    date_str,
  )
  return None, None


# ---------------- Visa ----------------
def _visa_traveller(
  http_session: requests.Session,
  date: dt.date,
  home: str,
  spend: str,
  amount: float = 1000,
) -> dict[str, Any] | None:
  """Looks up the Visa rate for spending abroad, billed at home.

  Args:
    http_session: The HTTP session used to send the request.
    date: The date for which to look up the rate.
    home: The cardholder billing currency code.
    spend: The transaction currency code.
    amount: The amount of ``spend`` currency to convert.

  Returns:
    A dictionary with keys ``rate`` (home per spend), ``bill``,
    and optionally ``network_benchmark``, or None if the Visa
    lookup fails. Cached results also include a ``cached`` flag set
    to True.
  """
  date_str = date.strftime(_DATE_FMT)

  month_day_year = date.strftime("%m/%d/%Y")
  json_body = _get(
    http_session,
    VISA_URL,
    VISA_HEADERS,
    {
      "amount": str(amount),
      "fee": "0",
      "utcConvertedDate": month_day_year,
      "exchangedate": month_day_year,
      "fromCurr": home,
      "toCurr": spend,
      "isISOCurr": "true",
    },
  )
  if not json_body or json_body.get("status") != "success":
    logger.warning(
      "Visa traveller lookup failed for %s (%s->%s)",
      date_str,
      home,
      spend,
    )
    return None

  original_values = json_body["originalValues"]
  first_benchmark = (original_values.get("benchmarks") or [{}])[0]
  rate = _to_float(original_values.get("fxRateVisa"))
  bill = _to_float(original_values.get("toAmountWithVisaRate"))
  visa_benchmark = _to_float(first_benchmark.get("benchmarkFxRate"))

  result = {
    "rate": rate,
    "bill": bill,
    "network_benchmark": visa_benchmark,
  }
  if rate:
    db_set(
      "visa",
      date_str,
      home,
      spend,
      {
        "rate": rate,
        "network_benchmark": visa_benchmark,
      },
    )
    if visa_benchmark is not None:
      db_set_benchmark("visa", date_str, spend, home, visa_benchmark)

  return result


def visa_traveller(
  http_session: requests.Session,
  dates: list[dt.date],
  home: str,
  spend: str,
  amount: float = 1000,
) -> list[dict[str, Any] | None]:
  """Looks up Visa rates for a batch of dates.

  Results are returned in the same order as ``dates``. Cached rows are loaded
  with one range query before missing dates are requested from Visa.
  """
  prefetch_benchmark_rates(http_session, dates, home, [spend])
  cached = _cached_range_hits("visa", dates, home, spend, amount)
  return [
    cached.get(date.strftime(_DATE_FMT))
    or _visa_traveller(http_session, date, home, spend, amount)
    for date in dates
  ]


# ---------------- Mastercard ----------------
def _mc_traveller(
  http_session: requests.Session,
  date: dt.date,
  home: str,
  spend: str,
  amount: float = 1000,
) -> dict[str, Any] | None:
  """Looks up the Mastercard rate for spending abroad, billed at home.

  Args:
    http_session: The HTTP session used to send the request.
    date: The date for which to look up the rate.
    home: The cardholder billing currency code.
    spend: The transaction currency code.
    amount: The amount of ``spend`` currency to convert.

  Returns:
    A dictionary with keys ``rate`` (home per spend) and ``bill``,
    or None if the lookup fails. Cached results also include a
    ``cached`` flag set to True.
  """
  date_str = date.strftime(_DATE_FMT)

  json_body = _get(
    http_session,
    MASTERCARD_URL,
    MASTERCARD_HEADERS,
    {
      "exchange_date": date_str,
      "transaction_currency": spend,
      "cardholder_billing_currency": home,
      "bank_fee": "0",
      "transaction_amount": str(amount),
    },
  )
  if (
    not json_body
    or "data" not in json_body
    or json_body["data"].get("errorCode")
  ):
    logger.warning(
      "Mastercard traveller lookup failed for %s (%s->%s)",
      date_str,
      home,
      spend,
    )
    return None

  response_data = json_body["data"]
  rate = _to_float(response_data.get("conversionRate"))
  bill = _to_float(response_data.get("crdhldBillAmt"))

  result = {"rate": rate, "bill": bill}
  if rate:
    db_set("mastercard", date_str, home, spend, {"rate": rate})
  return result


def mc_traveller(
  http_session: requests.Session,
  dates: list[dt.date],
  home: str,
  spend: str,
  amount: float = 1000,
) -> list[dict[str, Any] | None]:
  """Looks up Mastercard rates for a batch of dates.

  Results are returned in the same order as ``dates``. Cached rows are loaded
  with one range query before missing dates are requested from Mastercard.
  """
  prefetch_benchmark_rates(http_session, dates, home, [spend])
  cached = _cached_range_hits("mastercard", dates, home, spend, amount)
  return [
    cached.get(date.strftime(_DATE_FMT))
    or _mc_traveller(http_session, date, home, spend, amount)
    for date in dates
  ]


# ---------------- UnionPay ----------------
def _up_traveller(
  http_session: requests.Session,
  date: dt.date,
  home: str,
  spend: str,
  amount: float = 1000,
) -> dict[str, Any] | None:
  """Looks up the UnionPay rate for spending abroad, billed at home.

  Args:
    http_session: The HTTP session used to send the request.
    date: The date for which to look up the rate.
    home: The cardholder billing currency code.
    spend: The transaction currency code.
    amount: The amount of ``spend`` currency to convert.

  Returns:
    A dictionary with keys ``rate`` (home per spend) and ``bill``,
    or None if the rate is unavailable. Cached results also include
    a ``cached`` flag set to True.
  """
  date_str = date.strftime(_DATE_FMT)
  cached = db_get("unionpay", date_str, home, spend)
  if cached and cached.get("rate"):
    rate = cached["rate"]
    return {
      "rate": rate,
      "bill": round(rate * float(amount), 4),
      "cached": True,
    }

  date_key = date.strftime("%Y%m%d")
  json_body = _get(
    http_session,
    UNIONPAY_URL_TEMPLATE.format(date=date_key),
    UNIONPAY_HEADERS,
  )
  if not json_body or "exchangeRateJson" not in json_body:
    logger.warning(
      "UnionPay payload invalid or unavailable for %s",
      date_str,
    )
    return None

  rate = None
  for rate_entry in json_body.get("exchangeRateJson", []):
    if (
      rate_entry.get("baseCur", "").upper() == home.upper()
      and rate_entry.get("transCur", "").upper() == spend.upper()
    ):
      rate = _to_float(rate_entry.get("rateData"))
      break

  if rate is None:
    logger.warning(
      "UnionPay rate not found for pair %s/%s on %s",
      home,
      spend,
      date_str,
    )
    return None

  result = {"rate": rate, "bill": round(rate * float(amount), 4)}
  db_set("unionpay", date_str, home, spend, {"rate": rate})
  return result


def up_traveller(
  http_session: requests.Session,
  dates: list[dt.date],
  home: str,
  spend: str,
  amount: float = 1000,
) -> list[dict[str, Any] | None]:
  """Looks up UnionPay rates for a batch of dates.

  Results are returned in the same order as ``dates``. Cached rows are loaded
  with one range query before missing dates are requested from UnionPay.
  """
  prefetch_benchmark_rates(http_session, dates, home, [spend])
  cached = _cached_range_hits("unionpay", dates, home, spend, amount)
  return [
    cached.get(date.strftime(_DATE_FMT))
    or _up_traveller(http_session, date, home, spend, amount)
    for date in dates
  ]
