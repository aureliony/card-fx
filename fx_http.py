"""HTTP transport with browser impersonation, logging, and retries."""

import json
import logging
import time
from typing import Any

from curl_cffi import requests

logger = logging.getLogger(__file__)


def to_float(value: Any) -> float | None:
  """Convert a value to float, returning None for invalid values."""
  try:
    return float(value)
  except (TypeError, ValueError):
    return None


class HttpClient:
  """Executes JSON GET requests with bounded retries."""

  def __init__(self) -> None:
    self.session = requests.Session(impersonate="chrome")

  def get(
    self,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    tries: int = 4,
  ) -> Any | None:
    """Perform a GET request and decode its JSON response."""
    param_str = f" with params={params}" if params else ""
    for attempt in range(tries):
      attempt_str = f" (attempt {attempt + 1}/{tries})" if attempt else ""
      logger.info("[HTTP GET] Initiating request to %s%s%s", url, param_str, attempt_str)
      start_time = time.time()
      try:
        response = self.session.get(url, headers=headers, params=params, timeout=30)
        elapsed = time.time() - start_time
        logger.info("[HTTP %s] Completed in %.2fs from %s", response.status_code, elapsed, url)
        if response.status_code == 200:
          return response.json()
        if response.status_code in (400, 404):
          logger.warning(
            "[HTTP %s] Resource not found or not yet published at %s", response.status_code, url
          )
          return None
        logger.warning("[HTTP %s] Unexpected status response from %s", response.status_code, url)
      except requests.exceptions.RequestException as error:
        logger.warning(
          "[HTTP ERROR] Network exception after %.2fs: %s", time.time() - start_time, error
        )
      except json.JSONDecodeError as error:
        logger.error("[JSON ERROR] Failed to decode JSON from %s: %s", url, error)
      except Exception as error:  # pylint: disable=broad-except
        logger.error("[UNEXPECTED ERROR] %s", error)

      if attempt < tries - 1:
        sleep_seconds = 1.2 * (attempt + 1)
        logger.info("Retrying in %.1fs...", sleep_seconds)
        time.sleep(sleep_seconds)
    logger.error("[HTTP FAILED] Exhausted %d attempts for %s", tries, url)
    return None


http_client = HttpClient()
