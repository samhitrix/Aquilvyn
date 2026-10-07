import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libs" / "fm_common"))
for svc in ("identity", "portfolio", "market", "analytics", "advisor", "dashboard", "readiness"):
    sys.path.insert(0, str(ROOT / "services" / svc))
