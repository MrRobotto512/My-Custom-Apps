"""Restore Doctor launcher (double-click on Windows, or: python RestoreDoctor.pyw [--sim]).

    RestoreDoctor.pyw --station [--bench=http://benchtool.local:8377]
runs it as a restore station driven from the shop's Hero Dashboard instead.
"""
import sys

if __name__ == "__main__":
    if "--station" in sys.argv[1:]:
        from restore_doctor.station import main as station_main
        station_main(sys.argv[1:])
    else:
        from restore_doctor.ui.app import main
        main()
