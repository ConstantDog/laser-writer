"""English startup with a text-only loading window."""

import tkinter as tk
from tkinter import messagebox, ttk
import traceback


def main():
    root = tk.Tk()
    root.withdraw()
    loading = tk.Toplevel(root)
    loading.title("Laser Writer")
    loading.resizable(False, False)
    ttk.Label(loading, text="Loading Laser Writer...", padding=28).pack()
    loading.update_idletasks()
    w, h = loading.winfo_reqwidth(), loading.winfo_reqheight()
    loading.geometry(
        f"+{(root.winfo_screenwidth()-w)//2}+{(root.winfo_screenheight()-h)//2}"
    )
    loading.protocol("WM_DELETE_WINDOW", root.destroy)

    def initialize():
        try:
            from fluidnc_laser_writer.app import LaserWriterApp

            LaserWriterApp(root)
            loading.destroy()
            root.deiconify()
        except Exception:
            loading.destroy()
            messagebox.showerror("Startup failed", traceback.format_exc(), parent=root)
            root.destroy()

    root.after(50, initialize)
    root.mainloop()


if __name__ == "__main__":
    main()
