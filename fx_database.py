"""SQLite persistence for network and benchmark rates."""

import datetime as dt
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Iterable
from typing import Any

from fx_config import DATE_FMT, DB_PATH

logger = logging.getLogger(__file__)


class RateDatabase:
  """Stores and retrieves network and benchmark rates in SQLite."""

  _init_lock = threading.Lock()

  def __init__(self, path: str = DB_PATH) -> None:
    self.path = path
    self._init_db()

  def _get_connection(self) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(self.path))
    if directory and not os.path.exists(directory):
      os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(self.path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection

  def _init_db(self) -> None:
    if os.path.exists(self.path):
      return
    with self._init_lock:
      if os.path.exists(self.path):
        return
      with self._get_connection() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
          """
          CREATE TABLE IF NOT EXISTS network_rates (
            network TEXT NOT NULL, date TEXT NOT NULL, home TEXT NOT NULL,
            spend TEXT NOT NULL, rate REAL, network_benchmark REAL,
            saved_at TEXT, raw_data TEXT,
            PRIMARY KEY (network, date, home, spend)
          )
          """
        )
        connection.execute(
          "CREATE INDEX IF NOT EXISTS idx_network_rates_date " "ON network_rates (date)"
        )
        connection.execute(
          """
          CREATE TABLE IF NOT EXISTS benchmark_rates (
            source TEXT NOT NULL, date TEXT NOT NULL, base TEXT NOT NULL,
            quote TEXT NOT NULL, rate REAL NOT NULL, saved_at TEXT,
            raw_data TEXT,
            PRIMARY KEY (source, date, base, quote)
          )
          """
        )
        connection.execute(
          "CREATE INDEX IF NOT EXISTS idx_benchmark_rates_lookup "
          "ON benchmark_rates (date, base, quote)"
        )
        connection.commit()

  @staticmethod
  def _row_value(row: sqlite3.Row) -> dict[str, Any]:
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

  def get(self, network: str, date: str, home: str, spend: str) -> dict[str, Any] | None:
    try:
      with self._get_connection() as connection:
        row = connection.execute(
          "SELECT rate, network_benchmark, saved_at, raw_data "
          "FROM network_rates WHERE network = ? AND date = ? AND home = ? AND spend = ?",
          (network.lower(), date, home.upper(), spend.upper()),
        ).fetchone()
        if row:
          logger.debug("[CACHE HIT] %s | %s | %s/%s", network.upper(), date, home, spend)
          return self._row_value(row)
    except sqlite3.Error as error:
      logger.warning("Database error during get: %s", error)
    return None

  def get_range(
    self, network: str, home: str, spend: str, dates: list[dt.date]
  ) -> dict[str, dict[str, Any]]:
    date_strings = sorted({date.strftime(DATE_FMT) for date in dates})
    if not date_strings:
      return {}
    placeholders = ",".join("?" for _ in date_strings)
    result: dict[str, dict[str, Any]] = {}
    try:
      with self._get_connection() as connection:
        rows = connection.execute(
          "SELECT date, rate, network_benchmark, saved_at, raw_data "
          "FROM network_rates WHERE network = ? AND home = ? AND spend = ? "
          f"AND date IN ({placeholders}) ORDER BY date",
          [network.lower(), home.upper(), spend.upper(), *date_strings],
        ).fetchall()
        result = {row["date"]: self._row_value(row) for row in rows}
    except sqlite3.Error as error:
      logger.warning("Database error during range get: %s", error)
    return result

  def set(self, network: str, date: str, home: str, spend: str, record: dict[str, Any]) -> None:
    value = dict(record)
    value["_saved_at"] = value.get("_saved_at") or dt.datetime.now(dt.timezone.utc).isoformat()
    try:
      with self._get_connection() as connection:
        connection.execute(
          """
          INSERT INTO network_rates
            (network, date, home, spend, rate, network_benchmark, saved_at, raw_data)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(network, date, home, spend) DO UPDATE SET
            rate = excluded.rate, network_benchmark = excluded.network_benchmark,
            saved_at = excluded.saved_at, raw_data = excluded.raw_data
          """,
          (
            network.lower(),
            date,
            home.upper(),
            spend.upper(),
            value.get("rate"),
            value.get("network_benchmark") or value.get("benchmark"),
            value["_saved_at"],
            json.dumps(value, ensure_ascii=False),
          ),
        )
        connection.commit()
    except sqlite3.Error as error:
      logger.warning("Database error during set: %s", error)

  def get_benchmark(self, source: str, date: str, base: str, quote: str) -> float | None:
    try:
      with self._get_connection() as connection:
        row = connection.execute(
          "SELECT rate FROM benchmark_rates "
          "WHERE source = ? AND date = ? AND base = ? AND quote = ?",
          (source.lower(), date, base.upper(), quote.upper()),
        ).fetchone()
        return row["rate"] if row and row["rate"] is not None else None
    except sqlite3.Error as error:
      logger.warning("Database error during benchmark get: %s", error)
      return None

  def has_benchmark_rates(
    self,
    source: str,
    dates: list[dt.date],
    home: str,
    spend_currencies: Iterable[str] | None = None,
  ) -> bool:
    date_strings = sorted({date.strftime(DATE_FMT) for date in dates})
    if not date_strings:
      return False
    date_placeholders = ",".join("?" for _ in date_strings)
    home_code = home.upper()
    try:
      with self._get_connection() as connection:
        if spend_currencies:
          spend_list = [value.upper() for value in spend_currencies if value.upper() != home_code]
          if not spend_list:
            return True
          placeholders = ",".join("?" for _ in spend_list)
          row = connection.execute(
            "SELECT COUNT(DISTINCT base) FROM benchmark_rates "
            "WHERE source = ? AND quote = ? "
            f"AND date IN ({date_placeholders}) AND base IN ({placeholders})",
            [source.lower(), home_code, *date_strings, *spend_list],
          ).fetchone()
          return (row[0] if row else 0) >= len(set(spend_list))
        row = connection.execute(
          "SELECT 1 FROM benchmark_rates WHERE source = ? AND quote = ? "
          f"AND date IN ({date_placeholders}) LIMIT 1",
          [source.lower(), home_code, *date_strings],
        ).fetchone()
        return row is not None
    except sqlite3.Error as error:
      logger.warning("Database error during benchmark range check: %s", error)
      return False

  def set_benchmark(
    self, source: str, date: str, base: str, quote: str, rate: float, raw_data: Any = None
  ) -> None:
    self.set_benchmarks([(source, date, base, quote, rate, raw_data)])

  def set_benchmarks(self, records: Iterable[tuple[str, str, str, str, float, Any | None]]) -> None:
    rows = []
    saved_at = dt.datetime.now(dt.timezone.utc).isoformat()
    for source, date, base, quote, rate, raw_data in records:
      raw_value = (
        json.dumps(raw_data, ensure_ascii=False)
        if raw_data and not isinstance(raw_data, str)
        else raw_data
      )
      rows.append(
        (source.lower(), str(date), base.upper(), quote.upper(), rate, saved_at, raw_value)
      )
    if not rows:
      return
    try:
      with self._get_connection() as connection:
        connection.executemany(
          """
          INSERT INTO benchmark_rates
            (source, date, base, quote, rate, saved_at, raw_data)
          VALUES (?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(source, date, base, quote) DO UPDATE SET
            rate = excluded.rate, saved_at = excluded.saved_at, raw_data = excluded.raw_data
          """,
          rows,
        )
        connection.commit()
    except sqlite3.Error as error:
      logger.warning("Database error during benchmark batch set: %s", error)


database = RateDatabase()
