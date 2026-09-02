import logging
import re
import threading
from collections import deque
from logging.handlers import RotatingFileHandler

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_BUFFER: deque[str] = deque(maxlen=400)
_LOG_LOCK = threading.Lock()
_LOGGING_CONFIGURED = False
# Monotonic count of every record ever buffered. The Logs tab compares THIS,
# not len(buffer): once the ring is full its length pins at maxlen forever,
# which froze the tab permanently at the first 400 lines.
_LOG_SEQ = 0

# Size-capped crash-trace file: a runaway loop must never fill the disk, and
# unlike the in-memory ring buffer this survives the process dying.
_LOG_FILE_NAME = "sensarr.log"
_LOG_FILE_MAX_BYTES = 2 * 1024 * 1024  # ~2 MB
_LOG_FILE_BACKUP_COUNT = 2

_SECRET_PATTERNS = (
    # python-telegram-bot/httpx logs the complete Bot API URL at INFO unless
    # filtered; the path itself contains the bot credential.
    (re.compile(r"(https://api\.telegram\.org/bot)[^/\s]+", re.IGNORECASE),
     r"\1<redacted>"),
    (re.compile(r"([?&](?:api_key|token|X-Plex-Token)=)[^&\s]+",
                re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"(Authorization:\s*(?:Bearer|Basic)\s+)[^\s]+",
                re.IGNORECASE), r"\1<redacted>"),
)


def redact_log_text(text: str) -> str:
    redacted = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


class SecretRedactionFilter(logging.Filter):
    """Redact credentials before any stream, memory, or disk formatter runs."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_log_text(record.getMessage())
        record.args = ()
        return True


class InMemoryLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()

        with _LOG_LOCK:
            global _LOG_SEQ
            _LOG_SEQ += 1
            _LOG_BUFFER.append(message)


def log_sequence() -> int:
    """Total records ever logged (monotonic; survives ring-buffer wraparound)."""
    with _LOG_LOCK:
        return _LOG_SEQ


def configure_logging() -> None:
    global _LOGGING_CONFIGURED
    root_logger = logging.getLogger()
    if _LOGGING_CONFIGURED:
        return

    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT)
    secret_filter = SecretRedactionFilter()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(secret_filter)
    root_logger.addHandler(stream_handler)

    memory_handler = InMemoryLogHandler()
    memory_handler.setFormatter(formatter)
    memory_handler.addFilter(secret_filter)
    root_logger.addHandler(memory_handler)

    # app_paths is imported lazily here (not at module level) — app_paths
    # itself never imports app_logging, so there's no real cycle today, but
    # every other module in the app treats app_paths as a leaf resolved at
    # point of use, and this keeps that convention. A failure to open the
    # log file (permissions, read-only install, disk full, …) must degrade
    # to the pre-existing stream+memory behavior, never crash startup.
    try:
        import app_paths
        log_path = app_paths.PATHS.data_dir / _LOG_FILE_NAME
        file_handler = RotatingFileHandler(
            str(log_path), maxBytes=_LOG_FILE_MAX_BYTES,
            backupCount=_LOG_FILE_BACKUP_COUNT, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        file_handler.addFilter(secret_filter)
        root_logger.addHandler(file_handler)
    except Exception:
        root_logger.warning(
            "Could not set up the rotating log file (%s) — continuing with "
            "the in-memory/stream logs only.", _LOG_FILE_NAME, exc_info=True)

    # These libraries otherwise emit complete request URLs. Application-level
    # warnings/errors remain visible, while routine successful HTTP chatter no
    # longer duplicates sensitive endpoints into three rotating log files.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _LOGGING_CONFIGURED = True


def get_recent_logs() -> list[str]:
    with _LOG_LOCK:
        return list(_LOG_BUFFER)
