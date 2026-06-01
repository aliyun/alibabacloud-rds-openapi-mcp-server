#!/usr/bin/env python
"""Run real RDSAI calls through mocked IM bridge inputs.

Usage:
    ACCESS_KEY_ID=... ACCESS_SECRET=... python tests/e2e_rdsai_real.py

The script intentionally lives under tests/ for repeatable local validation,
but is not named test_*.py so normal unit discovery does not make network
calls.
"""

import sys
from pathlib import Path


INTEGRATION_DIR = Path(__file__).resolve().parents[1]
if str(INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_DIR))

from scripts.e2e_rdsai_bridges import main


if __name__ == "__main__":
    raise SystemExit(main())
