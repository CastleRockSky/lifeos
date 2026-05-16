"""
conftest.py — pytest bootstrap for the LifeOS API test suite.

The API modules use bare imports (`from database import get_pool`), so the
`api/` directory must be on sys.path. Running `pytest` from `api/` picks this
file up first and prepends that directory.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
