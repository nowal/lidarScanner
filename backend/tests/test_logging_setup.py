"""Exercise real rotation without replacing pytest's own root handlers."""
import subprocess
import sys
import os


def test_logs_rotate_and_successful_http_requests_are_suppressed(tmp_path):
    script = """
import logging, sys
from app import logging_setup
logging_setup.LOG_MAX_BYTES = 1024
logging_setup.LOG_BACKUP_COUNT = 2
logging_setup.configure_logging(sys.argv[1])
for i in range(250):
    logging.getLogger('httpx').info('successful request must not be retained')
    logging.getLogger('httpcore').info('connection noise must not be retained')
    logging.getLogger('lidarai').info('event %d %s', i, 'x' * 100)
logging.getLogger('httpx').warning('transport warning remains visible')
logging.getLogger('lidarai').error('application error remains visible')
logging.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                            env={**os.environ, "RENDER": "false"}, capture_output=True, text=True, check=True)
    files = list((tmp_path / "logs").iterdir())
    assert len(files) == 6
    assert all(path.stat().st_size <= 1024 for path in files)
    assert "successful request" not in result.stderr
    assert "connection noise" not in result.stderr
    for name in ("server.log", "server.jsonl"):
        retained = "".join(path.read_text() for path in (tmp_path / "logs").glob(name + "*"))
        assert "transport warning remains visible" in retained
        assert "application error remains visible" in retained
        assert "successful request" not in retained


def test_render_keeps_console_logs_without_creating_disk_copies(tmp_path):
    script = """
import logging, sys
from app.logging_setup import configure_logging
configure_logging(sys.argv[1])
configure_logging(sys.argv[1])
logging.getLogger('httpx').info('request noise')
logging.getLogger('lidarai').warning('important application warning')
logging.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                            env={**os.environ, "RENDER": "true"}, capture_output=True, text=True, check=True)
    assert result.stderr.count("important application warning") == 1
    assert "request noise" not in result.stderr
    assert list(tmp_path.iterdir()) == []
