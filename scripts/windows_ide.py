"""Run the Windows IDE without a console, with automatic recovery."""
import argparse
import os
from pathlib import Path
from common import read
from local import server_args
from windows_supervisor import supervise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('Windows only')
    root = args.root.resolve()
    config = read(root / 'local.json')
    supervise(root, server_args(root, config, windows=True), config['workspace'])
