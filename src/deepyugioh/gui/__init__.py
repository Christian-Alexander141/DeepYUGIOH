"""Test GUI for the Combo Generator - only available in test mode (see deepyugioh.settings)."""


def run_gui() -> int:
    from deepyugioh.gui.app import run_gui as _run  # tkinter is only imported when the GUI starts

    return _run()
