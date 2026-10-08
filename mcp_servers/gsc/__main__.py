import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # repo root, for `core`

from mcp_servers.gsc.server import main  # noqa: E402

main()
