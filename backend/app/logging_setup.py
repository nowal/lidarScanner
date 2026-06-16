import json
import logging
from datetime import datetime, timezone
from pathlib import Path


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
    logs_dir = Path(storage_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    text_handler = logging.StreamHandler()
    text_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    json_handler = logging.FileHandler(logs_dir / "server.jsonl")
    json_handler.setFormatter(JsonFormatter())

    human_handler = logging.FileHandler(logs_dir / "server.log")
    human_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    root.addHandler(text_handler)
    root.addHandler(json_handler)
    root.addHandler(human_handler)
