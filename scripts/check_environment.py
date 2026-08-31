from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from lgo.environment import check_environment  # noqa: E402
from lgo.settings import load_config  # noqa: E402


def main() -> None:
    report = check_environment(load_config())
    print(json.dumps(report, indent=2))

    if not report["ready_for_service"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

