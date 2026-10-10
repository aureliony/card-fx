"""Network-specific Visa, Mastercard, and UnionPay rate providers."""

import datetime as dt
from typing import Any

from fx_benchmarks import BenchmarkService, benchmark_service
from fx_config import (
  DATE_FMT,
  MASTERCARD_HEADERS,
  MASTERCARD_URL,
  UNIONPAY_HEADERS,
  UNIONPAY_URL_TEMPLATE,
  VISA_HEADERS,
  VISA_URL,
)
from fx_database import RateDatabase, database
from fx_http import HttpClient, http_client, to_float


class NetworkProvider:
  """Base class for providers that persist one network's rates."""

  def __init__(
    self, network: str, db: RateDatabase = database, client: HttpClient = http_client,
    benchmarks: BenchmarkService = benchmark_service,
  ) -> None:
    self.network = network
    self.db, self.client, self.benchmarks = db, client, benchmarks

  def _cached_range(
    self, dates: list[dt.date], home: str, spend: str, amount: float
  ) -> dict[str, dict[str, Any]]:
    cached_rows = self.db.get_range(self.network, home, spend, dates)
    results = {}
    for date_string, record in cached_rows.items():
      if record.get("rate"):
        results[date_string] = {
          "rate": record["rate"], "bill": round(record["rate"] * amount, 4),
          "cached": True,
        }
        if record.get("network_benchmark") is not None:
          results[date_string]["network_benchmark"] = record["network_benchmark"]
    return results

  def lookup(
    self, date: dt.date, home: str, spend: str,
    amount: float,
  ) -> dict[str, Any] | None:
    raise NotImplementedError

  def lookup_many(
    self, dates: list[dt.date], home: str,
    spend: str, amount: float = 100_000,
  ) -> list[dict[str, Any] | None]:
    self.benchmarks.prefetch(dates, home, [spend])
    cached = self._cached_range(dates, home, spend, amount)
    return [
      cached.get(date.strftime(DATE_FMT)) or self.lookup(date, home, spend, amount)
      for date in dates
    ]


class VisaProvider(NetworkProvider):
  def __init__(
    self, db: RateDatabase = database, client: HttpClient = http_client,
    benchmarks: BenchmarkService = benchmark_service,
  ) -> None:
    super().__init__("visa", db, client, benchmarks)

  def lookup(self, date, home, spend, amount):
    date_string = date.strftime(DATE_FMT)
    formatted_date = date.strftime("%m/%d/%Y")
    payload = self.client.get(VISA_URL, VISA_HEADERS, {
      "amount": str(amount), "fee": "0", "utcConvertedDate": formatted_date,
      "exchangedate": formatted_date, "fromCurr": home, "toCurr": spend, "isISOCurr": "true",
    })
    if not payload or payload.get("status") != "success":
      return None
    values = payload["originalValues"]
    benchmark = (values.get("benchmarks") or [{}])[0]
    rate = to_float(values.get("fxRateVisa"))
    bill = to_float(values.get("toAmountWithVisaRate"))
    network_benchmark = to_float(benchmark.get("benchmarkFxRate"))
    if rate:
      self.db.set("visa", date_string, home, spend,
                  {"rate": rate, "network_benchmark": network_benchmark})
      if network_benchmark is not None:
        self.db.set_benchmark("visa", date_string, spend, home, network_benchmark)
    return {"rate": rate, "bill": bill, "network_benchmark": network_benchmark}


class MastercardProvider(NetworkProvider):
  def __init__(
    self, db: RateDatabase = database, client: HttpClient = http_client,
    benchmarks: BenchmarkService = benchmark_service,
  ) -> None:
    super().__init__("mastercard", db, client, benchmarks)

  def lookup(self, date, home, spend, amount):
    date_string = date.strftime(DATE_FMT)
    payload = self.client.get(MASTERCARD_URL, MASTERCARD_HEADERS, {
      "exchange_date": date_string, "transaction_currency": spend,
      "cardholder_billing_currency": home, "bank_fee": "0",
      "transaction_amount": str(amount),
    })
    if not payload or "data" not in payload or payload["data"].get("errorCode"):
      return None
    values = payload["data"]
    rate, bill = to_float(values.get("conversionRate")), to_float(values.get("crdhldBillAmt"))
    if rate:
      self.db.set("mastercard", date_string, home, spend, {"rate": rate})
    return {"rate": rate, "bill": bill}


class UnionPayProvider(NetworkProvider):
  def __init__(
    self, db: RateDatabase = database, client: HttpClient = http_client,
    benchmarks: BenchmarkService = benchmark_service,
  ) -> None:
    super().__init__("unionpay", db, client, benchmarks)

  def lookup(self, date, home, spend, amount):
    date_string = date.strftime(DATE_FMT)
    cached = self.db.get(self.network, date_string, home, spend)
    if cached and cached.get("rate"):
      rate = cached["rate"]
      return {"rate": rate, "bill": round(rate * float(amount), 4), "cached": True}
    payload = self.client.get(
      UNIONPAY_URL_TEMPLATE.format(date=date.strftime("%Y%m%d")),
      UNIONPAY_HEADERS,
    )
    if not payload or "exchangeRateJson" not in payload:
      return None
    rate = next(
      (to_float(item.get("rateData")) for item in payload["exchangeRateJson"]
       if item.get("baseCur", "").upper() == home.upper()
       and item.get("transCur", "").upper() == spend.upper()),
      None,
    )
    if rate is None:
      return None
    self.db.set("unionpay", date_string, home, spend, {"rate": rate})
    return {"rate": rate, "bill": round(rate * float(amount), 4)}


visa_provider = VisaProvider()
mastercard_provider = MastercardProvider()
unionpay_provider = UnionPayProvider()
