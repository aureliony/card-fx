"""Command-line entry point for comparing card-network foreign-exchange markups."""

import argparse
import datetime as dt
import logging
from typing import Any

import numpy as np
from tabulate import tabulate

from fx_benchmarks import benchmark_service
from fx_networks import all_network_providers

logger = logging.getLogger(__file__)

# Currency list: ISO-4217 codes
CURRENCIES = [
  'AED', 'AUD', 'CAD', 'CHF', 'CNY', 'EUR', 'GBP', 'HKD', 'IDR', 'INR',
  'JPY', 'KRW', 'MYR', 'NZD', 'PHP', 'QAR', 'SAR', 'THB', 'TWD', 'USD',
  'VND'
]
SUPPORTED_CURRENCIES = [
  'AED', 'AFN', 'ALL', 'AMD', 'AOA', 'ARS', 'AUD', 'AWG', 'AZN', 'BAM',
  'BBD', 'BDT', 'BHD', 'BIF', 'BMD', 'BND', 'BOB', 'BRL', 'BSD', 'BTN',
  'BWP', 'BYN', 'BZD', 'CAD', 'CDF', 'CHF', 'CLP', 'CNY', 'COP', 'CRC',
  'CVE', 'CZK', 'DJF', 'DKK', 'DOP', 'DZD', 'EGP', 'ETB', 'EUR', 'FJD',
  'FKP', 'GBP', 'GEL', 'GHS', 'GIP', 'GMD', 'GNF', 'GTQ', 'GYD', 'HKD',
  'HNL', 'HTG', 'HUF', 'IDR', 'ILS', 'INR', 'IQD', 'ISK', 'JMD', 'JOD',
  'JPY', 'KES', 'KGS', 'KHR', 'KMF', 'KRW', 'KWD', 'KYD', 'KZT', 'LAK',
  'LBP', 'LKR', 'LRD', 'LSL', 'LYD', 'MAD', 'MDL', 'MGA', 'MKD', 'MMK',
  'MNT', 'MOP', 'MRU', 'MUR', 'MVR', 'MWK', 'MXN', 'MYR', 'MZN', 'NAD',
  'NGN', 'NIO', 'NOK', 'NPR', 'NZD', 'OMR', 'PAB', 'PEN', 'PGK', 'PHP',
  'PKR', 'PLN', 'PYG', 'QAR', 'RON', 'RSD', 'RUB', 'RWF', 'SAR', 'SBD',
  'SCR', 'SDG', 'SEK', 'SGD', 'SHP', 'SOS', 'SRD', 'SSP', 'SVC', 'SZL',
  'THB', 'TJS', 'TMT', 'TND', 'TOP', 'TRY', 'TTD', 'TWD', 'TZS', 'UAH',
  'UGX', 'USD', 'UYU', 'UZS', 'VES', 'VND', 'VUV', 'WST', 'XAF', 'XCD',
  'XCG', 'XOF', 'XPF', 'YER', 'ZAR', 'ZMW'
]


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
    default=CURRENCIES,
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
  spending_amount: float = 100_000,
) -> dict[str, Any] | None:
  """Gather rates and markups for a single spend currency."""
  markups_by_net = {provider.network: [] for provider in all_network_providers}
  valid_days = 0

  network_rates = [
    provider.lookup_many(dates, home_curr, spend_curr, spending_amount)
    for provider in all_network_providers
  ]

  for target_date, daily_rates in zip(
    dates, zip(*network_rates, strict=True), strict=True
  ):
    benchmark, _ = benchmark_service.get(
      target_date, home_curr, spend_curr
    )

    # Require rates for all networks and a neutral benchmark rate.
    if (
      benchmark is None
      or not all(rate_data and rate_data.get("rate") for rate_data in daily_rates)
    ):
      continue

    for provider, rate_data in zip(
      all_network_providers, daily_rates, strict=True
    ):
      rate = rate_data["rate"]
      markups_by_net[provider.network].append(
        ((rate - benchmark) / benchmark) * 100.0
      )
    valid_days += 1

  if valid_days < 3:
    return None

  # Calculate 25th, 50th (median), 75th percentiles of markups
  stats = {}
  for provider in all_network_providers:
    net = provider.network
    p25, p50, p75 = np.percentile(markups_by_net[net], [25, 50, 75])
    stats[net] = {"p25": p25, "p50": p50, "p75": p75}

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

  print("=" * 100)
  print(
    f"{days_back}-DAY FX SPREAD & BENCHMARK MARKUP COMPARISON "
    f"({len(currencies)} CURRENCIES)"
  )
  print(f"Card Billing Currency: {home_currency}")
  print("Baseline Benchmark   : Frankfurter (ECB Reference Rates)")
  print(
    f"Date Range           : {dates[-1].strftime('%Y-%m-%d')} to {dates[0].strftime('%Y-%m-%d')}"
  )
  print("=" * 100)

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
      res = analyze_currency(
        dates, home_currency, curr_code, args.spending_amount
      )

      if not res:
        print("Incomplete/insufficient benchmark data.")
        continue

      print(f"Done ({res['days']} trading days). Best: {res['best']}")
      wins_counter[res["best"]] += 1

      summary_table.append(
        [
          curr_code,
          *[
            (
              f"{res['stats'][provider.network]['p25']:+.2f} / "
              f"{res['stats'][provider.network]['p50']:+.2f} / "
              f"{res['stats'][provider.network]['p75']:+.2f}%"
            )
            for provider in all_network_providers
          ],
          res["best"],
        ]
      )

  headers = [
    "Currency",
    *[
      f"{provider.network} (P25/P50/P75)"
      for provider in all_network_providers
    ],
    "Lowest Spread",
  ]

  print("\n" + "=" * 105)
  print(f"{days_back}-DAY MARKUP OVER BENCHMARK PERCENTILE SUMMARY (Home: {home_currency})")
  print("Format: 25th% / Median (50th%) / 75th% Markup (Lower is better)")
  print("=" * 105)

  print(tabulate(summary_table, headers=headers, tablefmt="github"))

  # Leaderboard summary
  total_analyzed = len(summary_table)
  print("\n" + "=" * 105)
  print(
    "OVERALL LEADERBOARD "
    f"(Cheapest Median Markup across {len(currencies)} Currencies):"
  )
  for net, count in sorted(wins_counter.items(), key=lambda x: x[1], reverse=True):
    pct = (count / total_analyzed * 100) if total_analyzed else 0
    print(
      f"  {net:<12}: Best choice for {count:02d} / {total_analyzed} currencies ({pct:.1f}%)"
    )
  print("=" * 105)


if __name__ == "__main__":
  main()
