"""Configuration and shared constants for card-network FX lookups."""

import logging

logging.basicConfig(
  level=logging.INFO,
  format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
  datefmt="%H:%M:%S",
)

DB_PATH = "fx_db.sqlite"
DATE_FMT = "%Y-%m-%d"

FRANKFURTER_URL = "https://api.frankfurter.dev/v2"
VISA_URL = "https://www.visa.com.sg/cmsapi/fx/rates"
MASTERCARD_URL = (
  "https://www.mastercard.com/marketingservices/public/mccom-services/"
  "currency-conversions/conversion-rates"
)
UNIONPAY_URL_TEMPLATE = "https://www.unionpayintl.com/upload/jfimg/{date}.json"

FRANKFURTER_HEADERS = {
  "Accept": "application/json"
}
VISA_HEADERS = {
  "Accept": "application/json, text/plain, */*",
  "Referer": "https://www.visa.com.sg/support/consumer/travel-support/exchange-rate-calculator.html"
}
MASTERCARD_HEADERS = {
  "Accept": "*/*",
  "Referer": "https://www.mastercard.com/us/en/personal/get-support/currency-exchange-rate-converter.html"
}
UNIONPAY_HEADERS = {
  "Accept": "application/json, text/plain, */*",
  "Referer": "https://www.unionpayintl.com/sg/rate/",
}
