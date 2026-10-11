"""Configuration and shared constants for card-network FX lookups."""

DB_PATH = "fx_db.sqlite"
DATE_FMT = "%Y-%m-%d"

# Currency list: ISO-4217 codes
DEFAULT_CURRENCIES = [
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

FRANKFURTER_URL = "https://api.frankfurter.dev/v2"
VISA_URL = "https://www.visa.com.sg/cmsapi/fx/rates"
MASTERCARD_URL = (
  "https://www.mastercard.com/marketingservices/public/mccom-services/"
  "currency-conversions/conversion-rates"
)
UNIONPAY_URL_TEMPLATE = "https://www.unionpayintl.com/upload/jfimg/{date}.json"

FRANKFURTER_HEADERS = {"Accept": "application/json"}
VISA_HEADERS = {
  "Accept": "application/json, text/plain, */*",
  "Referer": "https://www.visa.com.sg/support/consumer/travel-support/exchange-rate-calculator.html",
}
MASTERCARD_HEADERS = {
  "Accept": "*/*",
  "Referer": "https://www.mastercard.com/us/en/personal/get-support/currency-exchange-rate-converter.html",
}
UNIONPAY_HEADERS = {
  "Accept": "application/json, text/plain, */*",
  "Referer": "https://www.unionpayintl.com/sg/rate/",
}
