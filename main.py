"""Command-line entry point for comparing card-network foreign-exchange markups."""

import argparse
import datetime as dt
import logging
from typing import Any

import numpy as np
from tabulate import tabulate

from fx.benchmarks import benchmark_service
from fx.config import DEFAULT_CURRENCIES, SUPPORTED_CURRENCIES
from fx.networks import all_network_providers

logger = logging.getLogger(__file__)

REPORT_WIDTH = 105
PERCENTILES = (0, 25, 50, 75, 100)
PERCENTILE_KEYS = tuple(f"p{percentile}" for percentile in PERCENTILES)
PERCENTILE_HEADER = "/".join(f"P{percentile}" for percentile in PERCENTILES)
MARKUP_FORMAT = " / ".join("{:+.2f}" for _ in PERCENTILES) + "%"
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


def analyze_currency(
  dates: list[dt.date],
  home_curr: str,
  spend_curr: str,
  spending_amount: float,
) -> dict[str, Any] | None:
  """Gather rates and markups for a single spend currency."""
  markups_by_net = {provider.network: [] for provider in all_network_providers}
  valid_days = 0

  network_rates = [
    provider.lookup_many(dates, home_curr, spend_curr, spending_amount)
    for provider in all_network_providers
  ]

  for target_date, daily_rates in zip(dates, zip(*network_rates, strict=True), strict=True):
    benchmark, _ = benchmark_service.get(target_date, home_curr, spend_curr)

    # Require rates for all networks and a neutral benchmark rate.
    if benchmark is None or not all(
      rate_data and rate_data.get("rate") for rate_data in daily_rates
    ):
      continue

    for provider, rate_data in zip(all_network_providers, daily_rates, strict=True):
      rate = rate_data["rate"]
      markups_by_net[provider.network].append(((rate - benchmark) / benchmark) * 100.0)
    valid_days += 1

  if valid_days < 3:
    return None

  # Calculate 0th, 25th, 50th (median), 75th, and 100th percentiles of markups
  stats = {}
  for provider in all_network_providers:
    net = provider.network
    percentile_values = np.percentile(markups_by_net[net], PERCENTILES)
    stats[net] = dict(zip(PERCENTILE_KEYS, percentile_values, strict=True))

  # Best network determined by lowest median markup
  best_network = min(stats, key=lambda k: stats[k]["p50"])

  return {
    "code": spend_curr,
    "days": valid_days,
    "stats": stats,
    "best": best_network,
  }


def main(argv: list[str] | None = None) -> None:
  """Run the configured FX comparison and print percentile and leaderboard results."""
  args = parse_args(argv)
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
  print("Baseline Benchmark   : Frankfurter (ECB Reference Rates)")
  print(
    f"Date Range           : {dates[-1].strftime('%Y-%m-%d')} to {dates[0].strftime('%Y-%m-%d')}"
  )
  print_separator()

  summary_table = []
  wins_counter = {provider.network: 0 for provider in all_network_providers}

  for idx, curr_code in enumerate(currencies, start=1):
    # Skip if spending currency is the same as home currency
    if curr_code == home_currency:
      continue

    print(
      f"[{idx:02d}/{len(currencies)}] Processing {curr_code}...",
      end=" ",
      flush=True,
    )
    res = analyze_currency(dates, home_currency, curr_code, args.spending_amount)

    if not res:
      print("Incomplete/insufficient benchmark data.")
      continue

    print(f"Done ({res['days']} trading days). Best: {res['best']}")
    wins_counter[res["best"]] += 1

    summary_table.append(
      [
        curr_code,
        *[
          format_markup_stats(res["stats"][provider.network])
          for provider in all_network_providers
        ],
        res["best"],
      ]
    )

  headers = [
    "Currency",
    *[f"{provider.network} ({PERCENTILE_HEADER})" for provider in all_network_providers],
    "Lowest Spread",
  ]

  print_separator(leading_newline=True)
  print(f"{days_back}-DAY MARKUP OVER BENCHMARK PERCENTILE SUMMARY (Home: {home_currency})")
  print(f"Format: {PERCENTILE_DESCRIPTION} Markup (Lower is better)")
  print_separator()

  print(tabulate(summary_table, headers=headers, tablefmt="github"))

  # Leaderboard summary
  total_analyzed = len(summary_table)
  print_separator(leading_newline=True)
  print("OVERALL LEADERBOARD " f"(Cheapest Median Markup across {len(currencies)} Currencies):")
  for net, count in sorted(wins_counter.items(), key=lambda x: x[1], reverse=True):
    pct = (count / total_analyzed * 100) if total_analyzed else 0
    print(f"  {net:<12}: Best choice for {count:02d} / {total_analyzed} currencies ({pct:.1f}%)")
  print_separator()


if __name__ == "__main__":
  main()
