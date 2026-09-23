"""Compatibility wrapper: run the consolidated UI regression suite."""
from pathlib import Path
import runpy
runpy.run_path(str(Path(__file__).with_name('browser_dual_destination.py')), run_name='__main__')
