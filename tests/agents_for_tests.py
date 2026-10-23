"""Importable agent used by the supervisor test (must be picklable)."""
import os
import sys
import time


def crash_first_time(marker):
    if not os.path.exists(marker):
        open(marker, "w").close()
        sys.exit(3)
    with open(marker, "a") as fh:
        fh.write("restarted\n")
    time.sleep(60)
