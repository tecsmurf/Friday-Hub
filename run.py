#!/usr/bin/env python3
"""
Friday Hub source entry point.

Installed usage:
    friday status
    friday run "your task here"

Source checkout usage:
    python run.py status
    python run.py run "your task here"
"""

from friday.cli import main


if __name__ == "__main__":
    main()
