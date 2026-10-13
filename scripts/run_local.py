"""Start both local services; Ctrl+C stops both. No AWS configuration required."""
from pathlib import Path
import os
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    for port in (8000, 5173):
        try:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', port))
        except OSError:
            sys.exit(f'Port {port} is in use. Stop the previous local demo before starting another.')
    if not (ROOT / 'ReactAuction/node_modules/vite').exists():
        sys.exit('Run bash scripts/setup-local.sh first.')
    children = []
    def stop(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        children.append(subprocess.Popen([sys.executable, '-u', 'local/mock_api.py'], cwd=ROOT, start_new_session=True))
        # Explicit overrides ensure this launcher never selects a historical endpoint.
        env = {**os.environ, 'VITE_REACT_APP_API_BASE': 'http://127.0.0.1:8000', 'VITE_LOCAL_MOCK': 'true'}
        children.append(subprocess.Popen(['bash', 'scripts/npm.sh', 'run', 'dev', '--', '--host', '127.0.0.1', '--port', '5173', '--strictPort'], cwd=ROOT, env=env, start_new_session=True))
        print('Local demo: http://127.0.0.1:5173 — synthetic data, reset on restart. Ctrl+C stops both services.', flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.3)
        raise RuntimeError('A local service exited; stopping the other service.')
    except KeyboardInterrupt:
        print('\nStopping local demo.', flush=True)
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


if __name__ == '__main__':
    main()
