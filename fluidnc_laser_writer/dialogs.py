"""Plain English error dialogs without artwork."""

import tkinter as tk
from tkinter import messagebox, ttk


def show_connection_failure(parent, details=""):
    messagebox.showerror(
        "Connection failed",
        details or "Connect FluidNC and select its serial port.",
        parent=parent,
    )


def show_check_failure(parent, title, message):
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.transient(parent)
    dialog.geometry("680x380")
    dialog.minsize(400, 220)
    body = ttk.Frame(dialog, padding=12)
    body.pack(fill=tk.BOTH, expand=True)
    report = tk.Text(body, wrap="word", font=("Segoe UI", 10), borderwidth=1)
    scroll = ttk.Scrollbar(body, command=report.yview)
    report.configure(yscrollcommand=scroll.set)
    report.insert("1.0", message)
    report.configure(state=tk.DISABLED)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)
    report.pack(fill=tk.BOTH, expand=True)
    ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=(0, 12))
    dialog.bind("<Escape>", lambda _: dialog.destroy())
    dialog.grab_set()
    parent.wait_window(dialog)
