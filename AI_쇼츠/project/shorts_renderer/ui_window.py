import tkinter as tk


def set_window_title(widget: tk.Misc, title: str) -> None:
    """Set title safely for both root windows and child Frame widgets."""
    if hasattr(widget, "title"):
        try:
            widget.title(title)
            return
        except Exception:
            pass

    top_level = widget.winfo_toplevel()
    if hasattr(top_level, "title"):
        top_level.title(title)

