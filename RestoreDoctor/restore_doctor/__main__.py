import sys

if "--station" in sys.argv[1:]:
    # station mode never touches the Tk UI module (a headless Pi has no Tk)
    from .station import main as _station
    _station(sys.argv[1:])
else:
    from .ui.app import main
    main()
