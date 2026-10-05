"""Exercise real Windows file locks across processes, not a mocked fcntl."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.bot_relay import TurnBusyError, acquire_turn_lock

pytestmark = pytest.mark.platforms("windows")

HOLDER = '''
import sys
from tools.bot_relay import acquire_turn_lock
with acquire_turn_lock(sys.argv[1], 'recipient', timeout_seconds=1):
    print('locked', flush=True)
    sys.stdin.readline()
'''


@pytest.fixture
def holder(tmp_path):
    # The runner's environment has already stripped credentials and isolated HOME.
    proc = subprocess.Popen([sys.executable, '-c', HOLDER, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[2], env=os.environ.copy(),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    # Bound startup without timing assertions; a dead/hung helper cannot hang pytest.
    import concurrent.futures
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        assert pool.submit(proc.stdout.readline).result(timeout=30).strip() == 'locked'
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)
        pool.shutdown(wait=True)


def test_same_profile_waits_but_other_profile_runs(tmp_path, holder):
    with pytest.raises(TurnBusyError):
        with acquire_turn_lock(tmp_path, 'recipient', timeout_seconds=0.15):
            pytest.fail('two processes acquired the same recipient')
    with acquire_turn_lock(tmp_path, 'different-recipient', timeout_seconds=0):
        pass


def test_dead_owner_releases_lock_without_deleting_file(tmp_path, holder):
    holder.kill()
    holder.wait(timeout=10)
    with acquire_turn_lock(tmp_path, 'recipient', timeout_seconds=1) as path:
        assert path.is_file()
