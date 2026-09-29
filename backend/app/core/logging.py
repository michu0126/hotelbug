import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit


def safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        path = parsed.path
        if parsed.hostname == "api.telegram.org":
            path = re.sub(r"^/bot[^/]+", "/bot[REDACTED]", path)
        return f"{parsed.scheme}://{parsed.hostname or ''}{path}"
    except ValueError:
        return "<invalid-url>"


def redact(value: str) -> str:
    value = re.sub(r'https?://[^\s"<>]+', lambda m: safe_url(m[0]), value)
    return re.sub(
        r"(?i)(authorization|cookie|password|token|secret|session)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", value
    )[:1000]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": redact(record.getMessage()),
        }
        for key in ("provider", "hotel_id", "task", "status", "latency", "error_type"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        return json.dumps(data, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
