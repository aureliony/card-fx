# Card FX

Card FX is a Python project that analyzes and compares currency exchange rates offered by major card networks: Visa, Mastercard, and UnionPay. It leverages publicly available API endpoints and web scraping techniques to fetch daily exchange rates. The primary goal is to provide users with insights into the real-world cost of foreign transactions by comparing network rates against official benchmarks and identifying potential markups.

## Features

*   **Multi-Network Comparison:** Fetches and compares rates from Visa, Mastercard, and UnionPay.
*   **Benchmark Analysis:** Uses [Frankfurter](https://frankfurter.dev/) as a baseline to calculate network markups.
*   **Historical Data Analysis:** Collects rate data over a specified period (e.g., 30 days) to identify trends and statistical performance.
*   **Percentile & Markup Calculation:** Computes 25th, 50th (median), and 75th percentile markups over the benchmark rate.
*   **Best Network Identification:** Determines which network offers the lowest median markup and which network was cheapest most often over the analysis period.
*   **Local SQLite Caching:** Stores fetched rate data locally in an SQLite3 database to speed up subsequent requests, support efficient indexing, and eliminate file-rewrite bottlenecks.
*   **Asynchronous HTTP:** Uses `curl_cffi.requests.AsyncSession` with concurrent provider requests, a configurable limit of four connections per host, and non-blocking retries.
*   **Impersonation:** Uses `curl_cffi` to mimic real browser requests, helping to bypass basic bot detection.
*   **Logging:** Integrated logging to track network requests, cache hits, and potential errors.

## Command-line usage

Run the default 30-day comparison using SGD as the card billing currency:

```bash
python main.py
```

The analysis can be customized with command-line options:

```bash
python main.py --home-currency USD --days-back 7 --spending-amount 250 --currencies EUR GBP JPY
```

The default spending amount is `100,000` in the spending currency. Use
`python main.py --help` to see all available options.

## Credits

*   **[anxkhn/best_rates](https://github.com/anxkhn/best_rates)** — The Visa and Mastercard API call logic in this project is based on the work from this repository.
