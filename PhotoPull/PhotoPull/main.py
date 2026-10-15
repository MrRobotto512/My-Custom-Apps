"""PhotoPull - copy photos & videos off a trusted iPhone/iPad to this PC."""
import multiprocessing

from photopull.gui import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
