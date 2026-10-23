"""Put the fakes and the canonical v1 sources on sys.path."""
import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
V1 = os.path.join(REPO, "DYODE_v1_full")
V2_IN = os.path.join(REPO, "DYODE_v2_light", "in")
V2_OUT = os.path.join(REPO, "DYODE_v2_light", "out")
FAKES = os.path.join(TESTS, "fakes")

for p in (V1, FAKES):
    if p not in sys.path:
        sys.path.insert(0, p)
