"""Local, bounded command logs for diagnosing instrument readback failures."""
import json
import threading
from pathlib import Path

from PySide6.QtCore import QStandardPaths

_log_lock = threading.Lock()


def save_control_log(data):
    root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)) / 'logs'
    path = root / 'lockin-control.jsonl'
    record = {'control': data.get('control'), 'readback': data.get('settings'),
              'display': data.get('display')}
    with _log_lock:
        root.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 2_000_000:
            path.replace(path.with_suffix('.previous.jsonl'))
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
    return str(path)
