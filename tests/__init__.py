"""Keep the suite away from the real install: default settings point at a throwaway
folder, so no test can read real keys or move a real Chelys-era folder (config.py)."""

import os
import tempfile

_sandbox = tempfile.mkdtemp(prefix="chelus-tests-")
for _name in ("CONFIG_DIR", "DATA_DIR"):
    os.environ[f"CHELUS_{_name}"] = os.path.join(_sandbox, _name.lower())
    os.environ.pop(f"CHELYS_{_name}", None)
