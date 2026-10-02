from __future__ import annotations
import collections
import csv
import math
import statistics
import time
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from .pd_controller import PDSample, PDSerialController
from .pd_reconstruction import reconstruct_high_curve
from .serial_controller import SerialPortInfo, available_ports
from .toolbar import NavigationToolbar


class PDMonitorWindow:
    """Independent PD acquisition window using a second USB serial device."""

    def __init__(
        self,
        parent: tk.Tk,
        *,
        reserved_port_provider: Callable[[], str | None],
        fluidnc_busy_provider: Callable[[], bool],
        on_closed: Callable[[], None],
    ) -> None:
        self.parent = parent
        self.reserved_port_provider = reserved_port_provider
        self.fluidnc_busy_provider = fluidnc_busy_provider
        self.on_closed = on_closed
        self.window = tk.Toplevel(parent)
        self.window.title("Independent ESP32 PD Monitor")
        self.window.geometry("1120x660")
        self.window.minsize(840, 560)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.port_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="Wi-Fi")
        self.host_var = tk.StringVar(value="192.168.4.1")
        self.tcp_port_var = tk.StringVar(value="9000")
        self._events = queue.Queue()
        self._connecting = False
        self.baud_var = tk.StringVar(value="230400")
        self.connection_var = tk.StringVar(value="PD acquisition disconnected")
        self.raw_var = tk.StringVar(value="—")
        self.mv_var = tk.StringVar(value="—")
        self.rate_var = tk.StringVar(value="0 samples/s")
        self.stats_var = tk.StringVar(value="min— | mean— | max—")
        self.window_seconds_var = tk.StringVar(value="10")
        self.buffer_var = tk.StringVar(value="buffered 0 | dropped 0")
        self._ports: dict[str, SerialPortInfo] = {}
        self._history: collections.deque[PDSample] = collections.deque(maxlen=200000)
        self._last_draw = 0.0
        self._closed = False
        self.controller = PDSerialController(
            on_connection=lambda connected, text: self._ui(
                self._handle_connection, connected, text
            ),
            on_message=lambda text: self._ui(self._append_console, text),
        )
        self._build_ui()
        self.refresh_ports()
        self.window.after(50, self._tick)

    @property
    def connected_port(self) -> str | None:
        return self.controller.port

    def focus(self) -> None:
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()

    def _build_ui(self) -> None:
        header = ttk.LabelFrame(
            self.window, text="Independent PD acquisition ESP32", padding=8
        )
        header.pack(fill=tk.X, padx=8, pady=8)
        ttk.Button(header, text="Refresh ports", command=self.refresh_ports).grid(
            row=0, column=0, padx=(0, 5)
        )
        self.port_combo = ttk.Combobox(
            header, textvariable=self.port_var, state="readonly", width=38
        )
        self.port_combo.grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Label(header, text="Baud").grid(row=0, column=2, padx=(12, 3))
        ttk.Entry(header, textvariable=self.baud_var, width=10).grid(row=0, column=3)
        self.connect_button = ttk.Button(
            header, text="Connect PD ESP32", command=self.toggle_connection
        )
        self.connect_button.grid(row=0, column=4, padx=8)
        ttk.Label(header, textvariable=self.connection_var).grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(5, 0)
        )
        header.columnconfigure(1, weight=1)
        network = ttk.Frame(header)
        network.grid(row=2, column=0, columnspan=5, sticky="ew", pady=5)
        ttk.Label(network, text="Transport").pack(side=tk.LEFT)
        ttk.Combobox(
            network,
            textvariable=self.mode_var,
            values=("Wi-Fi", "USB"),
            state="readonly",
            width=8,
        ).pack(side=tk.LEFT, padx=5)
        ttk.Label(network, text="IP / Host").pack(side=tk.LEFT)
        ttk.Entry(network, textvariable=self.host_var, width=18).pack(
            side=tk.LEFT, padx=5
        )
        ttk.Label(network, text="TCP").pack(side=tk.LEFT)
        ttk.Entry(network, textvariable=self.tcp_port_var, width=6).pack(
            side=tk.LEFT, padx=5
        )
        stats = ttk.Frame(self.window, padding=(10, 2))
        stats.pack(fill=tk.X)
        ttk.Label(
            stats, text="Reconstructed voltage:", font=("Segoe UI", 9, "bold")
        ).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Label(
            stats, textvariable=self.mv_var, width=12, font=("Consolas", 12, "bold")
        ).pack(side=tk.LEFT)
        ttk.Label(stats, textvariable=self.rate_var).pack(side=tk.LEFT, padx=14)
        ttk.Label(stats, textvariable=self.stats_var).pack(side=tk.LEFT, padx=14)
        ttk.Label(stats, text="Window/s").pack(side=tk.RIGHT, padx=(8, 3))
        ttk.Entry(stats, textvariable=self.window_seconds_var, width=6).pack(
            side=tk.RIGHT
        )
        action = ttk.Frame(self.window, padding=(10, 4))
        action.pack(fill=tk.X)
        ttk.Button(action, text="Clear plot", command=self.clear).pack(side=tk.LEFT)
        ttk.Button(action, text="Save buffered CSV", command=self.save_csv).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Label(action, textvariable=self.buffer_var).pack(side=tk.LEFT, padx=12)
        ttk.Label(
            action,
            text="Low-cluster rejection + linear interpolation; gaps over 200 ms remain blank. Not measured mean power.",
            foreground="#555555",
            wraplength=540,
            justify=tk.LEFT,
        ).pack(side=tk.RIGHT)
        self.figure = Figure(figsize=(8, 3.7), dpi=100)
        self.axis = self.figure.add_subplot(111)
        self.axis.set_title(
            "Waiting for PD data" + ": PD,<device_ms>,<raw>,<millivolts>"
        )
        self.axis.set_xlabel("Recent time/s")
        self.axis.set_ylabel("PD")
        self.axis.set_ylim(0.0, 1.0)
        self.axis.grid(True, linewidth=0.3, alpha=0.4)
        (self.line_artist,) = self.axis.plot([], [], color="#c2185b", linewidth=1.0)
        self.canvas = FigureCanvasTkAgg(self.figure, master=self.window)
        toolbar = NavigationToolbar(self.canvas, self.window, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(fill=tk.X, padx=8)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=8)
        self.canvas.draw_idle()
        console_frame = ttk.LabelFrame(
            self.window, text="PD serial messages (non-sample lines)", padding=4
        )
        console_frame.pack(fill=tk.X, padx=8, pady=(4, 8))
        self.console = tk.Text(
            console_frame,
            height=3,
            wrap="word",
            font=("Consolas", 9),
            state=tk.DISABLED,
        )
        self.console.pack(fill=tk.X)

    def _ui(self, callback, *args) -> None:
        if self._closed:
            return
        self._events.put((callback, args))

    def refresh_ports(self) -> None:
        reserved = self.reserved_port_provider()
        current = self.controller.port
        ports = [
            item
            for item in available_ports()
            if item.device != reserved or item.device == current
        ]
        self._ports = {item.display_name: item for item in ports}
        names = list(self._ports)
        self.port_combo["values"] = names
        if names and self.port_var.get() not in self._ports:
            self.port_var.set(names[0])
        elif not names:
            self.port_var.set("")

    def toggle_connection(self) -> None:
        if self._connecting:
            return
        if self.controller.connected:
            self.controller.disconnect()
            return
        if self.mode_var.get() == "Wi-Fi":
            try:
                host, port = (self.host_var.get(), int(self.tcp_port_var.get()))
            except ValueError:
                messagebox.showerror(
                    "PD", "TCP port must be an integer", parent=self.window
                )
                return
            self._connecting = True
            self.connect_button.configure(state="disabled")
            self.connection_var.set("Connecting over Wi-Fi…")

            def connect():
                error = None
                try:
                    self.controller.connect_wifi(host, port)
                except Exception as exc:
                    error = str(exc)
                if self._closed:
                    self.controller.disconnect()
                else:
                    self._ui(self._finish_connect, error)

            threading.Thread(
                target=connect, daemon=True, name="pd-wifi-connect"
            ).start()
            return
        selected = self._ports.get(self.port_var.get())
        if not selected:
            messagebox.showwarning(
                "No port selected",
                "Select the serial port of the second ESP32.",
                parent=self.window,
            )
            return
        reserved = self.reserved_port_provider()
        if reserved and selected.device == reserved:
            messagebox.showerror(
                "Port conflict",
                "This port is being used by the FluidNC controller.",
                parent=self.window,
            )
            return
        try:
            baud = int(self.baud_var.get())
            self.controller.connect(selected.device, baud)
        except Exception as exc:
            messagebox.showerror("PD connection failed", str(exc), parent=self.window)

    def _finish_connect(self, error):
        self._connecting = False
        self.connect_button.configure(state="normal")
        if error:
            self.connection_var.set("Wi-Fi connection failed")
            messagebox.showerror("PD Wi-Fi", error, parent=self.window)

    def _handle_connection(self, connected: bool, text: str) -> None:
        self.connection_var.set(text)
        self.connect_button.configure(
            text="Disconnect PD" if connected else "Connect PD ESP32"
        )

    def _tick(self) -> None:
        if self._closed:
            return
        for _ in range(100):
            try:
                callback, args = self._events.get_nowait()
            except queue.Empty:
                break
            callback(*args)
        samples = self.controller.drain_samples()
        if samples:
            self._history.extend(samples)
        self.buffer_var.set(
            f"buffered {len(self._history)} | buffer drops {self.controller.dropped_samples} | invalid frames {self.controller.invalid_frames}"
        )
        now = time.monotonic()
        render_interval = 0.2 if self.fluidnc_busy_provider() else 0.1
        if samples and now - self._last_draw >= render_interval:
            self._redraw()
            self._last_draw = now
        try:
            self.window.after(50, self._tick)
        except tk.TclError:
            pass

    def _visible_samples(self) -> list[PDSample]:
        if not self._history:
            return []
        try:
            seconds = min(60.0, max(0.5, float(self.window_seconds_var.get())))
        except ValueError:
            seconds = 10.0
        latest = self._history[-1].host_time_s
        cutoff = latest - seconds
        reverse_result: list[PDSample] = []
        for sample in reversed(self._history):
            if sample.host_time_s < cutoff:
                break
            reverse_result.append(sample)
        reverse_result.reverse()
        return reverse_result

    def _redraw(self) -> None:
        visible = self._visible_samples()
        if not visible:
            return
        latest_time = visible[-1].host_time_s
        y_values = reconstruct_high_curve(visible)
        all_y_values = [v for v in y_values if math.isfinite(v)]
        x_values = [sample.host_time_s - latest_time for sample in visible]
        valid = [i for i, v in enumerate(y_values) if math.isfinite(v)]
        if valid and latest_time - visible[valid[-1]].host_time_s <= 0.2:
            self.mv_var.set(f"{y_values[valid[-1]]:.1f} mV")
        else:
            self.mv_var.set("—")
        unit = visible[-1].plot_unit
        self.line_artist.set_data(x_values, y_values)
        self.axis.relim()
        self.axis.autoscale_view(scalex=True, scaley=False)
        self.axis.set_ylim(0.0, max(1.0, max(all_y_values, default=0.0)))
        self.axis.set_xlabel("Time relative to latest sample/s")
        self.axis.set_ylabel(unit)
        self.axis.set_title(
            f"PD reconstructed high curve ({unit}, low rejection + linear interpolation)"
        )
        self.canvas.draw_idle()
        if len(visible) >= 2:
            duration = visible[-1].host_time_s - visible[0].host_time_s
            rate = (len(visible) - 1) / duration if duration > 0 else 0.0
        else:
            rate = 0.0
        self.rate_var.set(f"{rate:.1f} samples/s")
        if not all_y_values:
            self.stats_var.set("—")
            return
        self.stats_var.set(
            f"min {min(all_y_values):.2f} | mean {statistics.fmean(all_y_values):.2f} | max {max(all_y_values):.2f} {unit}"
        )

    def clear(self) -> None:
        self._history.clear()
        self.controller.clear_samples()
        self.line_artist.set_data([], [])
        self.axis.relim()
        self.axis.autoscale_view(scalex=True, scaley=False)
        self.axis.set_ylim(0.0, 1.0)
        self.canvas.draw_idle()
        self.raw_var.set("—")
        self.mv_var.set("—")
        self.rate_var.set("0 samples/s")

    def save_csv(self) -> None:
        if self.fluidnc_busy_provider():
            messagebox.showwarning(
                "Writing in progress",
                "To avoid disk and CSV conversion load, export after FluidNC stops. PD samples remain buffered.",
                parent=self.window,
            )
            return
        if not self._history:
            messagebox.showwarning(
                "No data", "There are no PD samples.", parent=self.window
            )
            return
        filename = filedialog.asksaveasfilename(
            parent=self.window,
            title="Save PD data",
            defaultextension=".csv",
            initialfile="pd_samples.csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
        )
        if not filename:
            return
        try:
            with Path(filename).open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    ["host_time_s", "device_time_ms", "reconstructed_millivolts"]
                )
                history = list(self._history)
                for sample, value in zip(history, reconstruct_high_curve(history)):
                    writer.writerow(
                        [
                            f"{sample.host_time_s:.6f}",
                            (
                                ""
                                if sample.device_time_ms is None
                                else f"{sample.device_time_ms:.3f}"
                            ),
                            "" if not math.isfinite(value) else f"{value:.3f}",
                        ]
                    )
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc), parent=self.window)

    def _append_console(self, text: str) -> None:
        self.console.configure(state=tk.NORMAL)
        self.console.insert(tk.END, text + "\n")
        self.console.see(tk.END)
        self.console.configure(state=tk.DISABLED)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.controller.disconnect()
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        self.on_closed()
