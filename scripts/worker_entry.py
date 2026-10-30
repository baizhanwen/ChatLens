"""PyInstaller entry; imports remain explicit for frozen worker discovery."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.worker.server import main
if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
