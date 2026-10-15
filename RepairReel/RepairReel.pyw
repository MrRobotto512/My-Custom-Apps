# Double-click launcher for the RepairReel window (use run.bat so the venv is used).
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from repairreel.gui import main
main()
