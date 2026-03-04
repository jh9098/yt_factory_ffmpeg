"""Simple tooltip widget for tkinter controls."""

import tkinter as tk
from typing import Optional


class ToolTip:
    def __init__(self, widget: tk.Widget, text: str):
        self.widget = widget
        self.text = text
        self.tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None):
        if self.tip or not self.text.strip():
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self.tip,
            text=self.text,
            justify="left",
            bg="#1f2329",
            fg="#e6edf3",
            relief="solid",
            bd=1,
            padx=8,
            pady=4,
        )
        label.pack()

    def _hide(self, _event=None):
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None
