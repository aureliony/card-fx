"""Network-specific Visa, Mastercard, and UnionPay rate providers."""

import asyncio
import datetime as dt
from abc import abstractmethod
from typing import Any

from fx.config import (
  DATE_FMT,
  MASTERCARD_HEADERS,
  MASTERCARD_URL,
  UNIONPAY_HEADERS,
  UNIONPAY_URL_TEMPLATE,
  VISA_HEADERS,
  VISA_URL,
)
from fx.database import RateDatabase, database
from fx.http import HttpClient, to_float


class NetworkProvider:
  """Base class for providers that persist one network's rates."""

  def __init__(
    self,
    network: str,
    client: HttpClient,
    db: RateDatabase = database,
  ) -> None:
    self.network = network
    self.db = db
    self.client = client

  def _cached_range(
    self, dates: list[dt.date], home: str, spend: str, amount: float
  ) -> dict[str, dict[str, Any]]:
    cached_rows = self.db.get_range(self.network, home, spend, dates)
    results = {}
    for date_string, record in cached_rows.items():
      if record.get("rate"):
        results[date_string] = {
          "rate": record["rate"],
          "bill": round(record["rate"] * amount, 4),
          "cached": True,
        }
        if record.get("network_benchmark") is not None:
          results[date_string]["network_benchmark"] = record["network_benchmark"]
    return results

  @abstractmethod
  async def lookup(
    self,
    date: dt.date,
    home: str,
    spend: str,
    amount: float,
  ) -> dict[str, Any] | None:
    raise NotImplementedError

  async def lookup_many(
    self,
    dates: list[dt.date],
    home: str,
    spend: str,
    amount: float,
  ) -> list[dict[str, Any] | None]:
    cached = self._cached_range(dates, home, spend, amount)
    pending = [
      self.lookup(date, home, spend, amount)
      for date in dates
      if date.strftime(DATE_FMT) not in cached
    ]
    fetched = await asyncio.gather(*pending)
    fetched_iter = iter(fetched)
    return [
      cached.get(date.strftime(DATE_FMT)) or next(fetched_iter)
      for date in dates
    ]


class VisaProvider(NetworkProvider):
  def __init__(
    self,
    client: HttpClient,
    db: RateDatabase = database,
  ) -> None:
    super().__init__("visa", client, db)

  async def lookup(self, date, home, spend, amount):
    date_string = date.strftime(DATE_FMT)
    formatted_date = date.strftime("%m/%d/%Y")
    payload = await self.client.get(
      VISA_URL,
      VISA_HEADERS,
      {
        "amount": str(amount),
        "fee": "0",
        "utcConvertedDate": formatted_date,
        "exchangedate": formatted_date,
        "fromCurr": home,
        "toCurr": spend,
        "isISOCurr": "true",
      },
    )
    if not payload or payload.get("status") != "success":
      return None
    values = payload["originalValues"]
    benchmark = (values.get("benchmarks") or [{}])[0]
    rate = to_float(values.get("fxRateVisa"))
    bill = to_float(values.get("toAmountWithVisaRate"))
    network_benchmark = to_float(benchmark.get("benchmarkFxRate"))
    if rate:
      self.db.set(
        "visa", date_string, home, spend, {"rate": rate, "network_benchmark": network_benchmark}
      )
      if network_benchmark is not None:
        self.db.set_benchmark("visa", date_string, spend, home, network_benchmark)
    return {"rate": rate, "bill": bill, "network_benchmark": network_benchmark}


class MastercardProvider(NetworkProvider):
  def __init__(
    self,
    client: HttpClient,
    db: RateDatabase = database,
  ) -> None:
    super().__init__("mastercard", client, db)

  async def lookup(self, date, home, spend, amount):
    date_string = date.strftime(DATE_FMT)
    payload = await self.client.get(
      MASTERCARD_URL,
      MASTERCARD_HEADERS,
      {
        "exchange_date": date_string,
        "transaction_currency": spend,
        "cardholder_billing_currency": home,
        "bank_fee": "0",
        "transaction_amount": str(amount),
      },
    )
    if not payload or "data" not in payload or payload["data"].get("errorCode"):
      return None
    values = payload["data"]
    rate, bill = to_float(values.get("conversionRate")), to_float(values.get("crdhldBillAmt"))
    if rate:
      self.db.set("mastercard", date_string, home, spend, {"rate": rate})
    return {"rate": rate, "bill": bill}


class UnionPayProvider(NetworkProvider):
  def __init__(
    self,
    client: HttpClient,
    db: RateDatabase = database,
  ) -> None:
    super().__init__("unionpay", client, db)

  async def lookup(self, date, home, spend, amount):
    date_string = date.strftime(DATE_FMT)
    cached = self.db.get(self.network, date_string, home, spend)
    if cached and cached.get("rate"):
      rate = cached["rate"]
      return {"rate": rate, "bill": round(rate * float(amount), 4), "cached": True}
    payload = await self.client.get(
      UNIONPAY_URL_TEMPLATE.format(date=date.strftime("%Y%m%d")),
      UNIONPAY_HEADERS,
    )
    if not payload or "exchangeRateJson" not in payload:
      return None
    rate = None
    for item in payload["exchangeRateJson"]:
      base = item.get("baseCur", "")
      trans = item.get("transCur", "")
      item_rate = to_float(item.get("rateData"))
      self.db.set(self.network, date_string, base, trans, {"rate": item_rate})
      if rate is None and base.upper() == home.upper() and trans.upper() == spend.upper():
        rate = item_rate
    if rate is None:
      return None
    return {"rate": rate, "bill": round(rate * float(amount), 4)}


def create_network_providers(client: HttpClient) -> list[NetworkProvider]:
  """Create all network providers sharing one HTTP client."""
  return [
    VisaProvider(client=client),
    MastercardProvider(client=client),
    UnionPayProvider(client=client),
  ]
