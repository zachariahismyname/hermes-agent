"""Exact-id mailbox reads ride out a transient Windows sharing violation.

Tickets are read without the dir lock while other processes atomically replace them. On Windows such a
read can fail with a sharing violation (winerror 32), which aborted a whole receipt-recovery pass (S4
soak-05, 10 concurrent bots) and could fail a sender's ``await_delivery`` wait mid-turn. A real handle
opened with no sharing reproduces the OS state; it is released after a short hold, as a replace is.
A hold that outlasts the bounded retry still fails closed.
"""

import ctypes
import json
import threading
import time

import pytest

from tools import bot_live_delivery as bld

DELIVERY_ID = "ab" * 32


def _hold_without_sharing(path, seconds, ready):
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ, share none
    if handle in (None, ctypes.c_void_p(-1).value):
        return  # ``ready`` stays unset; the test fails on its wait instead of hanging.
    try:
        ready.set()
        time.sleep(seconds)
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _ticket(tmp_path):
    root = tmp_path / "runtime" / "bot_live_delivery"
    root.mkdir(parents=True)
    record = {"delivery_id": DELIVERY_ID, "status": "settled", "reply": "DONE"}
    (root / f"{DELIVERY_ID}.json").write_text(json.dumps(record), encoding="utf-8")
    return root / f"{DELIVERY_ID}.json", record


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("hold_s, readable", [(0.3, True), (2.0, False)])
def test_read_delivery_result_rides_out_a_transient_sharing_violation(tmp_path, hold_s, readable):
    path, record = _ticket(tmp_path)
    ready = threading.Event()
    holder = threading.Thread(target=_hold_without_sharing, args=(path, hold_s, ready), daemon=True)
    holder.start()
    try:
        assert ready.wait(10), "could not open the ticket without sharing"
        if readable:
            assert bld.read_delivery_result(tmp_path, DELIVERY_ID) == record
        else:
            with pytest.raises(PermissionError):
                bld.read_delivery_result(tmp_path, DELIVERY_ID)
    finally:
        holder.join(10)
