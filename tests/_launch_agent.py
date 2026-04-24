"""Helper: launch hybro-hub agent start as a standalone script.

Usage: python tests/_launch_agent.py agent start claude-code --port 9010 --working-dir /tmp
"""
import sys
from hub.cli import main

sys.exit(main())
