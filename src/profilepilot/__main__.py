"""``python -m profilepilot`` - the ProfilePilot command line (see :mod:`profilepilot.cli`).

This is also the MCPB bundle's entry point; clients start the MCP server with
``python -m profilepilot serve``.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
