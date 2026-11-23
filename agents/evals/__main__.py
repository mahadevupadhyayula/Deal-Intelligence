"""`python -m agents.evals` entry point.

WHAT: Delegates to `cli.main`.
WHERE: Operator/CI tooling.
WHY: Standard module-runner shim so the CLI needs no console-script install.
"""
import sys

from agents.evals.cli import main

sys.exit(main())
