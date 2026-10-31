"""Slack incoming-webhook delivery; webhook secrets never appear in errors."""

from email.utils import parsedate_to_datetime
import json
import math
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from monitor_config import ENV_FILE, secret_value


class SlackDeliveryError(ValueError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def load_webhook_url(env_file=ENV_FILE):
    value = secret_value("SLACK_WEBHOOK_URL", env_file)
    if value is None:
        return None
    try:
        parts = urlsplit(value)
        path = parts.path.split("/")
        valid = (parts.scheme == "https" and parts.netloc in {"hooks.slack.com", "hooks.slack-gov.com"}
                 and not parts.query and not parts.fragment and len(path) == 5
                 and path[1] == "services" and all(path[2:]))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("SLACK_WEBHOOK_URL must be a valid Slack incoming-webhook HTTPS URL")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise SlackDeliveryError("Slack webhook redirected; delivery deferred")


def retry_seconds(value):
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, delay) if math.isfinite(delay) else 0


def send_webhook(webhook_url, title, sections):
    # Plain-text blocks and escaped fallback text keep API-provided labels
    # from becoming Slack mentions. Include full details in the text fallback.
    fallback = (title + "\n\n" + "\n\n".join(sections))[:3900]
    payload = {
        "text": fallback.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
        "blocks": [{"type": "header", "text": {"type": "plain_text", "text": title[:150]}}]
        + [{"type": "section", "text": {"type": "plain_text", "text": section[:2900]}}
           for section in sections],
        "unfurl_links": False, "unfurl_media": False,
    }
    request = Request(webhook_url, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            status = response.status
            body = response.read(1024).decode("utf-8").strip()
    except HTTPError as error:
        delay = retry_seconds(error.headers.get("Retry-After")) if error.headers else 0
        status = error.code
        error.close()
        raise SlackDeliveryError(f"Slack delivery failed (HTTP {status})", delay) from None
    except (URLError, OSError, UnicodeError, ValueError):
        raise SlackDeliveryError("Slack delivery failed (network or invalid response)") from None
    if status != 200 or body != "ok":
        raise SlackDeliveryError("Slack did not confirm delivery")
