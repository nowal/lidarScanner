import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "job_id"):
            payload["jobId"] = record.job_id
        return json.dumps(payload)



def configure_logging(storage_dir: str) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()

    # Successful HTTP client requests dominate idle traffic. Keep failures
    # visible without recording every background database read.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    text_handler = logging.StreamHandler()
    text_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(text_handler)

    # Render already captures the console. Local duplicate files consume its
    # 2 GB /tmp volume, and rotating shared files is unsafe across web workers.
    if os.environ.get("RENDER") == "true":
        return

    logs_dir = Path(storage_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    json_handler = RotatingFileHandler(
        logs_dir / "server.jsonl", maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
    )
    json_handler.setFormatter(JsonFormatter())

    human_handler = RotatingFileHandler(
        logs_dir / "server.log", maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
    )
    human_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    root.addHandler(json_handler)
    root.addHandler(human_handler)
