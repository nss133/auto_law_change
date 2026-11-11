"""Run-scoped collection warnings (including worker-thread failures)."""
from threading import Lock

_lock = Lock()
warnings = []

def reset():
    with _lock:
        warnings.clear()

def warn(message):
    with _lock:
        warnings.append(str(message))
    print(f'[law_change_auto] 경고: {message}', flush=True)

def snapshot():
    with _lock:
        return list(warnings)
