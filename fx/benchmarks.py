"""Benchmark-rate fetching, caching, and fallback rules."""

import datetime as dt
import logging
from collections.abc import Iterable

from fx.config import DATE_FMT, FRANKFURTER_HEADERS, FRANKFURTER_URL
from fx.database import RateDatabase, database
from fx.http import HttpClient, to_float

logger = logging.getLogger(__file__)


class BenchmarkService:
  """Coordinates Frankfurter benchmark rates."""

  def __init__(self, client: HttpClient, db: RateDatabase = database) -> None:
    self.source = "frankfurter"
    self.db = db
    self.client = client

  async def prefetch(
    self,
    dates: list[dt.date],
    home: str,
    spend_currencies: Iterable[str] | None = None,
  ) -> int:
    date_strings = sorted({date.strftime(DATE_FMT) for date in dates})
    if not date_strings:
      return 0
    home_code = home.upper()
    if self.db.has_benchmark_rates(self.source, dates, home_code, spend_currencies):
      logger.info(
        "Benchmark rates for %s (%s to %s) already cached in database. " "Skipping API prefetch.",
        home_code,
        date_strings[0],
        date_strings[-1],
      )
      return 0
    payload = await self.client.get(
      f"{FRANKFURTER_URL}/rates",
      FRANKFURTER_HEADERS,
      {"from": date_strings[0], "to": date_strings[-1], "base": home_code},
    )
    if not payload or not isinstance(payload, list):
      logger.warning("Failed to prefetch Frankfurter benchmark rates.")
      return 0

    spend_filter = None
    if spend_currencies:
      spend_filter = {value.upper() for value in spend_currencies}
      spend_filter.discard(home_code)
    records = []
    for item in payload:
      if item.get("date") not in date_strings:
        continue
      quote = item.get("quote", "").upper()
      raw_rate = to_float(item.get("rate"))
      if not quote or quote == home_code or (spend_filter and quote not in spend_filter):
        continue
      if not raw_rate or raw_rate <= 0:
        continue
      records.append(
        (self.source, item["date"], quote, home_code, 1.0 / raw_rate, item)
      )
    if records:
      self.db.set_benchmarks(records)
      logger.info("Prefetched and cached %d Frankfurter benchmark records.", len(records))
    return len(records)

  async def get(self, date: dt.date, home: str, spend: str) -> tuple[float | None, str | None]:
    date_string = date.strftime(DATE_FMT)
    home_code, spend_code = home.upper(), spend.upper()
    cached = self.db.get_benchmark(self.source, date_string, spend_code, home_code)
    if cached is not None:
      return cached, self.source
    payload = await self.client.get(
      f"{FRANKFURTER_URL}/rate/{spend_code}/{home_code}",
      FRANKFURTER_HEADERS,
      {"date": date_string},
    )
    rate = to_float(payload.get("rate")) if payload else None
    if rate is not None:
      self.db.set_benchmark(self.source, date_string, spend_code, home_code, rate, payload)
      logger.info(
        "Using Frankfurter benchmark for %s/%s on %s: %s", home_code, spend_code, date_string, rate
      )
      return rate, self.source

    logger.warning(
      "No benchmark rate available for %s/%s on %s", home_code, spend_code, date_string
    )
    return None, None
