"""Test-wide safety: the suite must never call the live Sarvam API.

A developer machine may have SARVAM_API_KEY exported for the demo. Drop it
before any test runs so end-to-end tests exercise the offline path; the
Sarvam tests set their own fake key and a mocked HTTP transport.
"""

import os

os.environ.pop("SARVAM_API_KEY", None)
