"""Command-line entry point for comparing card-network foreign-exchange markups."""

import argparse
import asyncio
import datetime as dt
import logging
from collections.abc import Sequence
from typing import Any

from tabulate import tabulate

from fx.benchmarks import BenchmarkService
from fx.config import DEFAULT_CURRENCIES, SUPPORTED_CURRENCIES
from fx.http import HttpClient
from fx.networks import NetworkProvider, create_network_providers

logging.basicConfig(
  level=logging.WARNING,
  format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
  datefmt="%H:%M:%S",
)
logger = logging.getLogger(__file__)

REPORT_WIDTH = 105
PERCENTILES = (25, 50, 75)
PERCENTILE_KEYS = tuple(f"p{percentile}" for percentile in PERCENTILES)
MARKUP_FORMAT = " / ".join("{:+.2f}" for _ in PERCENTILES)
PERCENTILE_DESCRIPTION = " / ".join(
  f"{percentile}th%" if percentile != 50 else "Median (50th%)" for percentile in PERCENTILES
)


def print_separator(leading_newline: bool = False) -> None:
  """Print the report section separator."""
  prefix = "\n" if leading_newline else ""
  print(prefix + "=" * REPORT_WIDTH)


def format_markup_stats(stats: dict[str, float]) -> str:
  """Format markup percentiles for a summary table cell."""
  return MARKUP_FORMAT.format(*(stats[key] for key in PERCENTILE_KEYS))


def percentile(values: Sequence[float], percentiles: Sequence[float]) -> list[float]:
  """Return percentiles using linear interpolation between sorted values."""
  if not values:
    raise ValueError("percentile requires at least one value")
  if any(not 0 <= value <= 100 for value in percentiles):
    raise ValueError("percentiles must be between 0 and 100")

  sorted_values = sorted(values)
  results = []
  for value in percentiles:
    rank = (value / 100) * (len(sorted_values) - 1)
    lower_index = int(rank)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    fraction = rank - lower_index
    results.append(
      sorted_values[lower_index] + (
        fraction * (sorted_values[upper_index] - sorted_values[lower_index])
      )
    )
  return results


def split_dates_by_day_type(dates: Sequence[dt.date]) -> dict[str, list[dt.date]]:
  """Split dates into weekday and weekend groups."""
  return {
    "Weekdays": [target_date for target_date in dates if target_date.weekday() < 5],
    "Weekends": [target_date for target_date in dates if target_date.weekday() >= 5],
  }


async def analyze_currency(
  dates: list[dt.date],
  home_curr: str,
  spend_curr: str,
  spending_amount: float,
  providers: Sequence[NetworkProvider],
  benchmarks: BenchmarkService,
) -> dict[str, Any] | None:
  """Gather rates and markups for a single spend currency."""
  markups_by_net = {provider.network: [] for provider in providers}
  valid_days = 0

  await benchmarks.prefetch(dates, home_curr, [spend_curr])
  network_rates = await asyncio.gather(*(
    provider.lookup_many(
      dates, home_curr, spend_curr, spending_amount
    )
    for provider in providers
  ))

  benchmark_rates = await asyncio.gather(*(
    benchmarks.get(target_date, home_curr, spend_curr)
    for target_date in dates
  ))
  for daily_rates, (benchmark, _) in zip(
    zip(*network_rates, strict=True), benchmark_rates, strict=True
  ):

    # Require rates for all networks and a neutral benchmark rate.
    if benchmark is None or not all(
      rate_data and rate_data.get("rate") for rate_data in daily_rates
    ):
      continue

    for provider, rate_data in zip(providers, daily_rates, strict=True):
      rate = rate_data["rate"]
      markups_by_net[provider.network].append(((rate - benchmark) / benchmark) * 100.0)
    valid_days += 1

  if valid_days < 3:
    return None

  # Calculate the 25th, 50th (median), and 75th percentiles of markups.
  stats = {}
  for provider in providers:
    net = provider.network
    percentile_values = percentile(markups_by_net[net], PERCENTILES)
    stats[net] = dict(zip(PERCENTILE_KEYS, percentile_values, strict=True))

  # Best network determined by lowest median markup
  best_network = min(stats, key=lambda k: stats[k]["p50"])

  return {
    "code": spend_curr,
    "days": valid_days,
    "stats": stats,
    "best": best_network,
  }


async def analyze_period(
  period_name: str,
  dates: Sequence[dt.date],
  home_currency: str,
  currencies: Sequence[str],
  spending_amount: float,
  providers: Sequence[NetworkProvider],
  benchmarks: BenchmarkService,
) -> None:
  """Analyze and print results for one date group."""
  spending_currencies = [curr_code for curr_code in currencies if curr_code != home_currency]
  print_separator(leading_newline=True)
  print(f"{period_name.upper()} ANALYSIS ({len(dates)} DAYS)")
  if not dates:
    print("No dates in this group.")
    return

  print(
    f"Date Range           : {dates[-1].strftime('%Y-%m-%d')} "
    f"to {dates[0].strftime('%Y-%m-%d')}"
  )

  summary_table = []
  wins_counter = {provider.network: 0 for provider in providers}
  currency_count = len(spending_currencies)

  async def analyze_with_progress(
    idx: int, curr_code: str
  ) -> dict[str, Any] | None:
    print(f"[{idx:02d}/{currency_count}] Processing {curr_code}...", flush=True)
    res = await analyze_currency(
      list(dates), home_currency, curr_code, spending_amount, providers, benchmarks
    )
    if not res:
      print(
        f"[{idx:02d}/{currency_count}] {curr_code}: "
        "Incomplete/insufficient benchmark data."
      )
    else:
      print(
        f"[{idx:02d}/{currency_count}] {curr_code}: Done ({res['days']} samples). "
        f"Best: {res['best']}"
      )
    return res

  results = await asyncio.gather(*(
    analyze_with_progress(idx, curr_code)
    for idx, curr_code in enumerate(spending_currencies, start=1)
  ))

  for curr_code, res in zip(spending_currencies, results, strict=True):
    if not res:
      continue

    wins_counter[res["best"]] += 1
    summary_table.append(
      [
        curr_code,
        *[
          format_markup_stats(res["stats"][provider.network])
          for provider in providers
        ],
        res["best"],
      ]
    )

  headers = [
    "Currency",
    *[provider.network for provider in providers],
    "Lowest Spread",
  ]

  print_separator()
  print(f"{period_name.upper()} MARKUP OVER BENCHMARK PERCENTILE SUMMARY (Home: {home_currency})")
  print(f"Format: {PERCENTILE_DESCRIPTION} Markup (Lower is better)")
  print_separator()
  print(tabulate(summary_table, headers=headers, tablefmt="github"))

  total_analyzed = len(summary_table)
  print_separator(leading_newline=True)
  print(f"{period_name.upper()} LEADERBOARD (Cheapest Median Markup):")
  for net, count in sorted(wins_counter.items(), key=lambda item: item[1], reverse=True):
    pct = (count / total_analyzed * 100) if total_analyzed else 0
    print(f"  {net:<12}: Best choice for {count:02d} / {total_analyzed} currencies ({pct:.1f}%)")
  print_separator()


async def main_async(args: argparse.Namespace) -> None:
  """Run weekday and weekend FX comparisons."""
  async with HttpClient() as client:
    benchmarks = BenchmarkService(client=client)
    providers = create_network_providers(client)

    home_currency = args.home_currency
    days_back = args.days_back
    yesterday = dt.date.today() - dt.timedelta(days=1)
    dates = [yesterday - dt.timedelta(days=i) for i in range(days_back)]
    currencies = args.currencies

    print_separator()
    print(
      f"{days_back}-DAY FX SPREAD & BENCHMARK MARKUP COMPARISON " f"({len(currencies)} CURRENCIES)"
    )
    print(f"Card Billing Currency: {home_currency}")
    print("Baseline Benchmark   : Frankfurter (Blended Reference Rates from 100+ Central Banks)")
    print(
      f"Date Range           : {dates[-1].strftime('%Y-%m-%d')} to {dates[0].strftime('%Y-%m-%d')}"
    )
    print_separator()

    spending_currencies = [curr_code for curr_code in currencies if curr_code != home_currency]
    if spending_currencies:
      await benchmarks.prefetch(dates, home_currency, spending_currencies)

    for period_name, period_dates in split_dates_by_day_type(dates).items():
      await analyze_period(
        period_name,
        period_dates,
        home_currency,
        currencies,
        args.spending_amount,
        providers,
        benchmarks,
      )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  """Parse command-line options for the FX comparison."""
  parser = argparse.ArgumentParser(
    description="Compare card-network FX markups against the Frankfurter benchmark."
  )
  parser.add_argument(
    "--home-currency",
    "-c",
    default="SGD",
    type=str.upper,
    choices=SUPPORTED_CURRENCIES,
    help="Card billing currency (default: SGD).",
  )
  parser.add_argument(
    "--days-back",
    "-d",
    default=30,
    type=int,
    metavar="DAYS",
    help="Number of previous days to analyze (default: 30).",
  )
  parser.add_argument(
    "--spending-amount",
    "-a",
    dest="spending_amount",
    default=100_000.0,
    type=float,
    help="Transaction amount in the spending currency (default: 100,000).",
  )
  parser.add_argument(
    "--currencies",
    "-C",
    nargs="+",
    default=DEFAULT_CURRENCIES,
    type=str.upper,
    choices=SUPPORTED_CURRENCIES,
    metavar="CURRENCY",
    help="Currencies to analyze (default: all supported spending currencies).",
  )
  args = parser.parse_args(argv)
  if args.days_back < 1:
    parser.error("--days-back must be at least 1")
  if args.spending_amount <= 0:
    parser.error("--spending-amount must be greater than 0")
  return args


def main(argv: list[str] | None = None) -> None:
  """Run the asynchronous CLI entry point."""
  args = parse_args(argv)
  asyncio.run(main_async(args))


if __name__ == "__main__":
  main()
