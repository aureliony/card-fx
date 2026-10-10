import datetime as dt
import logging
from typing import Any

import numpy as np
from tabulate import tabulate

import fx_api

logging.basicConfig(level=logging.INFO)

# Currency list: ISO-4217 codes
CURRENCIES = [
  'AED', 'AUD', 'CAD', 'CHF', 'CNY', 'EUR', 'GBP', 'HKD', 'IDR', 'INR',
  'JPY', 'KRW', 'MYR', 'NZD', 'PHP', 'QAR', 'SAR', 'THB', 'TWD', 'USD',
  'VND'
]


def analyze_currency(
  session: fx_api.requests.Session,
  dates: list[dt.date],
  home_curr: str,
  spend_curr: str,
  amount: float = 1000,
) -> dict[str, Any] | None:
  """Gathers 30-day rates and markups for a single spend currency."""
  markups_by_net = {"Visa": [], "Mastercard": [], "UnionPay": []}
  rates_by_net = {"Visa": [], "Mastercard": [], "UnionPay": []}
  valid_days = 0

  visa_rates = fx_api.visa_traveller(
    session, dates, home_curr, spend_curr, amount
  )
  mastercard_rates = fx_api.mc_traveller(
    session, dates, home_curr, spend_curr, amount
  )
  unionpay_rates = fx_api.up_traveller(
    session, dates, home_curr, spend_curr, amount
  )

  for target_date, v, m, u in zip(
    dates, visa_rates, mastercard_rates, unionpay_rates
  ):

    benchmark, _ = fx_api.get_benchmark_rate(session, target_date, home_curr, spend_curr)

    # Require rates for all three networks and a neutral benchmark rate
    if (
      v
      and m
      and u
      and v.get("rate")
      and m.get("rate")
      and u.get("rate")
      and benchmark is not None
    ):
      v_rate, m_rate, u_rate = v["rate"], m["rate"], u["rate"]

      # Calculate markup % over neutral benchmark
      v_markup = ((v_rate - benchmark) / benchmark) * 100.0
      m_markup = ((m_rate - benchmark) / benchmark) * 100.0
      u_markup = ((u_rate - benchmark) / benchmark) * 100.0

      markups_by_net["Visa"].append(v_markup)
      markups_by_net["Mastercard"].append(m_markup)
      markups_by_net["UnionPay"].append(u_markup)

      rates_by_net["Visa"].append(v_rate)
      rates_by_net["Mastercard"].append(m_rate)
      rates_by_net["UnionPay"].append(u_rate)

      valid_days += 1

  if valid_days < 3:
    return None

  # Calculate 25th, 50th (median), 75th percentiles of markups
  stats = {}
  for net in ["Visa", "Mastercard", "UnionPay"]:
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


def main() -> None:
  home_currency = "SGD"  # Card billing currency
  days_back = 30
  yesterday = dt.date.today() - dt.timedelta(days=1)
  dates = [yesterday - dt.timedelta(days=i) for i in range(days_back)]

  print("=" * 100)
  print(f"30-DAY FX SPREAD & BENCHMARK MARKUP COMPARISON ({len(CURRENCIES)} CURRENCIES)")
  print(f"Card Billing Currency: {home_currency}")
  print("Baseline Benchmark   : Frankfurter (ECB Reference Rates)")
  print(
    f"Date Range           : {dates[-1].strftime('%Y-%m-%d')} to {dates[0].strftime('%Y-%m-%d')}"
  )
  print("=" * 100)

  summary_table = []
  wins_counter = {"Visa": 0, "Mastercard": 0, "UnionPay": 0}

  with fx_api.session() as session:
    for idx, curr_code in enumerate(CURRENCIES, start=1):
      # Skip if spending currency is the same as home currency
      if curr_code == home_currency:
        continue

      print(
        f"[{idx:02d}/{len(CURRENCIES)}] Processing {curr_code}...",
        end=" ",
        flush=True,
      )
      res = analyze_currency(session, dates, home_currency, curr_code)

      if not res:
        print("Incomplete/insufficient benchmark data.")
        continue

      print(f"Done ({res['days']} trading days). Best: {res['best']}")
      wins_counter[res["best"]] += 1

      v_p = res["stats"]["Visa"]
      m_p = res["stats"]["Mastercard"]
      u_p = res["stats"]["UnionPay"]

      summary_table.append(
        [
          curr_code,
          # Visa 25 / 50 / 75
          f"{v_p['p25']:+.2f} / {v_p['p50']:+.2f} / {v_p['p75']:+.2f}%",
          # MC 25 / 50 / 75
          f"{m_p['p25']:+.2f} / {m_p['p50']:+.2f} / {m_p['p75']:+.2f}%",
          # UP 25 / 50 / 75
          f"{u_p['p25']:+.2f} / {u_p['p50']:+.2f} / {u_p['p75']:+.2f}%",
          res["best"],
        ]
      )

  headers = [
    "Currency",
    "Visa (P25/P50/P75)",
    "Mastercard (P25/P50/P75)",
    "UnionPay (P25/P50/P75)",
    "Lowest Spread",
  ]

  print("\n" + "=" * 105)
  print(f"30-DAY MARKUP OVER BENCHMARK PERCENTILE SUMMARY (Home: {home_currency})")
  print("Format: 25th% / Median (50th%) / 75th% Markup (Lower is better)")
  print("=" * 105)

  try:
    print(tabulate(summary_table, headers=headers, tablefmt="github"))
  except ImportError:
    header_line = "{:<25} | {:<25} | {:<25} | {:<25} | {:<12}".format(*headers)
    print(header_line)
    print("-" * len(header_line))
    for row in summary_table:
      print("{:<25} | {:<25} | {:<25} | {:<25} | {:<12}".format(*row))

  # Leaderboard summary
  total_analyzed = len(summary_table)
  print("\n" + "=" * 105)
  print("OVERALL LEADERBOARD (Cheapest Median Markup across 21 Currencies):")
  for net, count in sorted(wins_counter.items(), key=lambda x: x[1], reverse=True):
    pct = (count / total_analyzed * 100) if total_analyzed else 0
    print(
      f"  {net:<12}: Best choice for {count:02d} / {total_analyzed} currencies ({pct:.1f}%)"
    )
  print("=" * 105)


if __name__ == "__main__":
  main()
