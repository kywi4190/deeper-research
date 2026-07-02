"""CLI entry: `python -m deeper.schemas [--check]` regenerates/verifies schemas/."""

import sys

from .export import main

sys.exit(main())
