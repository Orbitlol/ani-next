"""Test suite for aninext.

Run with:

    python -m unittest discover -s tests -v
    ./tests/run.sh

No network access is required: providers are exercised against the built-in
offline demo site and against local HTTP servers started by the tests.
"""
import os
import sys
import tempfile

# keep tests away from the user's real ~/.aninext
_TMP_HOME = tempfile.mkdtemp(prefix="aninext-tests-")
os.environ.setdefault("ANINEXT_HOME", _TMP_HOME)
os.environ.setdefault("NO_COLOR", "1")
os.environ.setdefault("ANINEXT_LOG", "error")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
