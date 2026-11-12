"""Test GUIs - only available in test mode (see deepyugioh.settings).

Stage 1: the Combo Generator (gui/app.py). Stage 2: the Analyzer System (gui/analysis.py), same shell.
"""


def run_gui(stage: int = 1) -> int:
    # tkinter is only imported when a GUI starts
    if stage == 2:
        from deepyugioh.gui.analysis import run_gui as _run
    else:
        from deepyugioh.gui.app import run_gui as _run

    return _run()
