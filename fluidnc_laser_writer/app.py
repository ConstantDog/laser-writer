from __future__ import annotations
import bisect
import collections
import ctypes
import math
import re
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from matplotlib import rcParams
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.collections import LineCollection
from matplotlib.figure import Figure
from shapely import affinity
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.ops import unary_union
from .core import (
    APP_VERSION,
    DesignRules,
    DrcResult,
    GCodeProgram,
    GdsDocument,
    LayerInfo,
    PathSettings,
    analyze_gcode,
    build_gcode,
    generate_serpentine_plan,
    list_layers,
    load_gds,
    polygons_for_layer,
    run_drc,
    save_simulation_png,
    text_digest,
)
from .dialogs import show_check_failure, show_connection_failure
from .serial_controller import FluidNCController, SerialPortInfo, available_ports
from .pd_window import PDMonitorWindow
from .toolbar import NavigationToolbar

rcParams["font.sans-serif"] = [
    "DejaVu Sans",
    "DejaVu Sans",
    "Arial Unicode MS",
    "DejaVu Sans",
]
rcParams["axes.unicode_minus"] = False
_PARAMETER_ENGLISH = {
    "max_width": "Maximum width",
    "max_height": "Maximum height",
    "min_feature_um": "Minimum feature",
    "min_spacing_um": "Minimum spacing",
    "precision_um": "Target precision",
    "hatch_um": "Hatch spacing",
    "spot_um": "Simulated spot",
    "x_steps": "X steps/mm",
    "y_steps": "Y steps/mm",
    "feed": "Feed",
    "acceleration": "Acceleration",
    "power": "Laser power",
    "max_power": "Maximum laser power",
    "overscan": "Overscan",
    "settle_ms": "Row settle time",
    "machine_x": "X travel",
    "machine_y": "Y travel",
    "jog_step": "Jog step",
    "jog_feed": "Jog feed",
}


class LaserWriterApp:

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"Laser Writer v{APP_VERSION}")
        self._fit_main_window_to_work_area()
        self.document: GdsDocument | None = None
        self.layer_infos: dict[tuple[int, int], LayerInfo] = {}
        self.selected_layer: tuple[int, int] | None = None
        self.current_geometry = None
        self.last_drc: DrcResult | None = None
        self.generated_program: GCodeProgram | None = None
        self.generation_signature: tuple[float | int, ...] | None = None
        self.validated_digest: str | None = None
        self._setting_editor_text = False
        self._ports: dict[str, SerialPortInfo] = {}
        self._segment_line_indices: list[int] = []
        self._accepted_artist = None
        self._reported_artist = None
        self._accepted_line_index = -1
        self._executed_segment_cursor = 0
        self._last_wco: tuple[float, float] | None = None
        self._last_reported_power = 0.0
        self._status_timestamps: collections.deque[float] = collections.deque(maxlen=40)
        self.pd_window: PDMonitorWindow | None = None
        self.status_var = tk.StringVar(value="Select a GDS file to begin.")
        self.file_var = tk.StringVar(value="No GDS selected")
        self.cell_var = tk.StringVar()
        self.review_var = tk.BooleanVar(value=False)
        self.x_home_enabled = tk.BooleanVar(value=False)
        self.x_home_interval = tk.StringVar(value="10")
        self.original_generated_digest: str | None = None
        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")
        self.connection_var = tk.StringVar(value="Disconnected")
        self.machine_state_var = tk.StringVar(value="Unknown")
        self.position_var = tk.StringVar(value="X —   Y —")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_text_var = tk.StringVar(value="Not sent")
        self.accepted_line_var = tk.StringVar(value="FluidNC accepted line: —")
        self.executed_line_var = tk.StringVar(value="Estimated active line: —")
        self.execution_command_var = tk.StringVar(value="Estimated active command: —")
        self.laser_state_var = tk.StringVar(value="Laser command: unknown")
        self.status_rate_var = tk.StringVar(value="Status refresh: 0 Hz")
        self.gcode_summary_var = tk.StringVar(value="No G-code generated")
        self.simulation_summary_var = tk.StringVar(
            value="Red: exposure; blue: laser-off travel"
        )
        self.param_vars = {
            "max_width": tk.StringVar(value="15.0"),
            "max_height": tk.StringVar(value="15.0"),
            "min_feature_um": tk.StringVar(value="100"),
            "min_spacing_um": tk.StringVar(value="100"),
            "precision_um": tk.StringVar(value="100"),
            "hatch_um": tk.StringVar(value="25"),
            "spot_um": tk.StringVar(value="40"),
            "x_steps": tk.StringVar(value="200"),
            "y_steps": tk.StringVar(value="200"),
            "feed": tk.StringVar(value="5"),
            "acceleration": tk.StringVar(value="5"),
            "power": tk.StringVar(value="500"),
            "max_power": tk.StringVar(value="1000"),
            "overscan": tk.StringVar(value="0.200"),
            "settle_ms": tk.StringVar(value="20"),
            "machine_x": tk.StringVar(value="100"),
            "machine_y": tk.StringVar(value="100"),
            "jog_step": tk.StringVar(value="0.100"),
            "jog_feed": tk.StringVar(value="5"),
        }
        self._configure_style()
        self._build_ui()
        self.controller = FluidNCController(
            on_line=lambda line: self._ui(self._handle_serial_line, line),
            on_connection=lambda connected, text: self._ui(
                self._handle_connection, connected, text
            ),
            on_progress=lambda done, total, line_index: self._ui(
                self._handle_progress, done, total, line_index
            ),
            on_complete=lambda success, text: self._ui(
                self._handle_complete, success, text
            ),
        )
        for key, variable in self.param_vars.items():
            if key not in {"jog_step", "jog_feed"}:
                variable.trace_add("write", self._on_generation_parameter_changed)
        self.refresh_ports()
        self.root.after(500, self._poll_status)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _fit_main_window_to_work_area(self) -> None:
        """Keep the complete main window above the Windows taskbar."""
        self.root.update_idletasks()
        work_left = 0
        work_top = 0
        work_right = self.root.winfo_screenwidth()
        work_bottom = self.root.winfo_screenheight()
        if sys.platform == "win32":

            class Rect(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            rect = Rect()
            try:
                if ctypes.windll.user32.SystemParametersInfoW(
                    48, 0, ctypes.byref(rect), 0
                ):
                    work_left, work_top = (rect.left, rect.top)
                    work_right, work_bottom = (rect.right, rect.bottom)
            except (AttributeError, OSError):
                pass
        work_width = max(640, work_right - work_left)
        work_height = max(480, work_bottom - work_top)
        width = max(640, min(1480, work_width - 32))
        height = max(420, min(840, work_height - 72))
        x = work_left + max(12, (work_width - width) // 2)
        y = work_top + 12
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(min(1100, width), min(680, height))

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("TLabel", font=("Segoe UI", 8))
        style.configure("TButton", font=("Segoe UI", 8), padding=(4, 2))
        style.configure("TCheckbutton", font=("Segoe UI", 8))
        style.configure("TLabelframe.Label", font=("Segoe UI", 8, "bold"))
        style.configure("Treeview", font=("Segoe UI", 8), rowheight=20)
        style.configure("Treeview.Heading", font=("Segoe UI", 8, "bold"))
        style.configure("TNotebook.Tab", font=("Segoe UI", 8), padding=(7, 3))
        style.configure(
            "Emergency.TButton",
            foreground="#a4001d",
            background="#ffe7ec",
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "Emergency.TButton",
            foreground=[("active", "#7f0016"), ("pressed", "#5d0010")],
            background=[("active", "#ffd2dc"), ("pressed", "#ffb8c8")],
        )
        style.configure(
            "Pass.TLabel", foreground="#137333", font=("Segoe UI", 8, "bold")
        )
        style.configure(
            "Fail.TLabel", foreground="#b00020", font=("Segoe UI", 8, "bold")
        )

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(10, 8))
        header.pack(fill=tk.X)
        ttk.Label(header, text="Laser Writer", font=("Segoe UI", 12, "bold")).pack(
            side=tk.LEFT
        )
        ttk.Label(header, textvariable=self.connection_var, padding=(16, 0)).pack(
            side=tk.RIGHT
        )
        ttk.Button(header, text="PD Monitor", command=self.open_pd_window).pack(
            side=tk.RIGHT, padx=6
        )
        ttk.Button(
            header,
            text="FORCE STOP",
            style="Emergency.TButton",
            command=self.force_stop,
        ).pack(side=tk.RIGHT, padx=8)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.design_tab = ttk.Frame(self.notebook, padding=8)
        self.gcode_tab = ttk.Frame(self.notebook, padding=8)
        self.simulation_tab = ttk.Frame(self.notebook, padding=8)
        self.device_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.design_tab, text="1  Design")
        self.notebook.add(self.gcode_tab, text="2  Review")
        self.notebook.add(self.simulation_tab, text="3  Preview")
        self.notebook.add(self.device_tab, text="4  Machine")
        self._build_design_tab()
        self._build_gcode_tab()
        self._build_simulation_tab()
        self._build_device_tab()
        status_bar = ttk.Label(
            self.root,
            textvariable=self.status_var,
            anchor="w",
            padding=(10, 5),
            relief=tk.SUNKEN,
        )
        status_bar.pack(fill=tk.X)

    def _build_design_tab(self) -> None:
        file_row = ttk.Frame(self.design_tab)
        file_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(file_row, text="Open GDS file", command=self.open_gds).pack(
            side=tk.LEFT
        )
        ttk.Label(file_row, textvariable=self.file_var, anchor="w").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=10
        )
        body = ttk.Panedwindow(self.design_tab, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(body, padding=(0, 0, 8, 0))
        right = ttk.Frame(body)
        body.add(left, weight=3)
        body.add(right, weight=2)
        cell_row = ttk.Frame(left)
        cell_row.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(cell_row, text="Top Cell:").pack(side=tk.LEFT)
        self.cell_combo = ttk.Combobox(
            cell_row, textvariable=self.cell_var, state="readonly", width=42
        )
        self.cell_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.cell_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self.refresh_layers()
        )
        layer_frame = ttk.LabelFrame(left, text="Layer/Datatype", padding=5)
        layer_frame.pack(fill=tk.BOTH, expand=True)
        columns = ("layer", "datatype", "count", "width", "height", "precheck")
        self.layer_tree = ttk.Treeview(
            layer_frame, columns=columns, show="headings", selectmode="browse"
        )
        labels = {
            "layer": "Layer",
            "datatype": "Datatype",
            "count": "Polygons",
            "width": "Width/mm",
            "height": "Height/mm",
            "precheck": "Size check",
        }
        widths = {
            "layer": 70,
            "datatype": 75,
            "count": 80,
            "width": 100,
            "height": 100,
            "precheck": 100,
        }
        for column in columns:
            self.layer_tree.heading(column, text=labels[column])
            self.layer_tree.column(column, width=widths[column], anchor="center")
        scroll = ttk.Scrollbar(
            layer_frame, orient=tk.VERTICAL, command=self.layer_tree.yview
        )
        self.layer_tree.configure(yscrollcommand=scroll.set)
        self.layer_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.layer_tree.bind("<<TreeviewSelect>>", self._on_layer_selected)
        action_row = ttk.Frame(left)
        action_row.pack(fill=tk.X, pady=8)
        ttk.Button(action_row, text="Check design", command=self.run_drc_action).pack(
            side=tk.LEFT
        )
        ttk.Button(
            action_row, text="Generate G-code", command=self.generate_action
        ).pack(side=tk.LEFT, padx=8)
        drc_frame = ttk.LabelFrame(
            left, text="Design-rule and precision results", padding=4
        )
        drc_frame.pack(fill=tk.BOTH, expand=False)
        self.drc_text = tk.Text(
            drc_frame, height=10, wrap="word", font=("Consolas", 9), state=tk.DISABLED
        )
        self.drc_text.pack(fill=tk.BOTH, expand=True)
        self._build_parameters(right)

    def _build_parameters(self, parent: ttk.Frame) -> None:
        canvas = tk.Canvas(parent, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        content = ttk.Frame(canvas)
        content.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.bind(
            "<Configure>", lambda event: canvas.itemconfigure(window, width=event.width)
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        scroll_remainder = 0.0

        def scroll_parameters(event, precise=False):
            nonlocal scroll_remainder
            pointer_x, pointer_y = parent.winfo_pointerxy()
            hovered = parent.winfo_containing(pointer_x, pointer_y)
            while hovered is not None and hovered is not parent:
                hovered = getattr(hovered, "master", None)
            if hovered is not parent or not canvas.winfo_ismapped():
                scroll_remainder = 0.0
                return
            if canvas.yview() == (0.0, 1.0):
                return
            delta = getattr(event, "delta", 0)
            if precise:
                _, dy = canvas.tk.call("tk::PreciseScrollDeltas", delta)
                pixels = -float(dy)
            elif delta:
                pixels = -float(delta) * 48.0 / 120.0
            else:
                pixels = -48.0 if getattr(event, "num", 0) == 4 else 48.0
            if pixels * scroll_remainder < 0:
                scroll_remainder = 0.0
            scroll_remainder += pixels
            whole_pixels = math.trunc(scroll_remainder)
            scroll_remainder -= whole_pixels
            bounds = canvas.bbox("all")
            if whole_pixels and bounds and (bounds[3] > bounds[1]):
                height = bounds[3] - bounds[1]
                canvas.yview_moveto(canvas.yview()[0] + whole_pixels / height)
            return "break"

        self.root.bind("<MouseWheel>", scroll_parameters, add="+")
        self.root.bind("<Button-4>", scroll_parameters, add="+")
        self.root.bind("<Button-5>", scroll_parameters, add="+")
        touchpad_tag = "LaserWriterParameterTouchpad"
        try:
            self.root.bind_class(
                touchpad_tag,
                "<TouchpadScroll>",
                lambda event: scroll_parameters(event, precise=True),
            )
        except tk.TclError:
            pass
        ttk.Label(
            content, text="Parameters", wraplength=480, foreground="#174a75"
        ).pack(fill=tk.X, pady=(0, 8))
        groups = [
            (
                "A · Writing commands",
                "Regenerate G-code after changes.",
                [
                    ("Hatch spacing/µm", "hatch_um"),
                    ("Feed≤10/mm/min", "feed"),
                    ("Laser output S", "power"),
                    ("Overscan/mm", "overscan"),
                    ("Row dwell/ms", "settle_ms"),
                ],
            ),
            (
                "B · Coordinate planning references",
                "Match firmware steps/mm. These fields do not change hardware settings.",
                [
                    ("X reference steps/mm", "x_steps"),
                    ("Y reference steps/mm", "y_steps"),
                ],
            ),
            (
                "C · Validation thresholds",
                "Check limits only; geometry is not scaled.",
                [
                    ("Max width/mm", "max_width"),
                    ("Max height/mm", "max_height"),
                    ("Min feature/µm", "min_feature_um"),
                    ("Min spacing/µm", "min_spacing_um"),
                    ("Target precision/µm", "precision_um"),
                ],
            ),
            (
                "D · Firmware references / validation",
                "Reference values for path checks. Match the firmware configuration.",
                [
                    ("Reference acceleration≤10/mm/s²", "acceleration"),
                    ("Validation S maximum", "max_power"),
                    ("X validation travel/mm", "machine_x"),
                    ("Y validation travel/mm", "machine_y"),
                ],
            ),
            (
                "E · Preview only",
                "Display only; does not change the laser spot.",
                [("Simulated spot/µm", "spot_um")],
            ),
        ]
        for title, description, fields in groups:
            frame = ttk.LabelFrame(content, text=title, padding=8)
            frame.pack(fill=tk.X, pady=(0, 8))
            frame.columnconfigure(1, weight=1)
            ttk.Label(
                frame, text=description, wraplength=480, foreground="#555555"
            ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
            for row, (label, key) in enumerate(fields, start=1):
                ttk.Label(frame, text=label).grid(
                    row=row, column=0, sticky="w", padx=(0, 8), pady=3
                )
                ttk.Entry(frame, textvariable=self.param_vars[key], width=14).grid(
                    row=row, column=1, sticky="ew", pady=3
                )
        note = "Exposure and dark travel use the same feed. Homing rates are set in FluidNC."
        ttk.Label(
            content, text=note, wraplength=360, foreground="#555555", justify=tk.LEFT
        ).pack(fill=tk.X, padx=4)

        def bind_parameter_touchpad(widget):
            widget.bindtags((touchpad_tag,) + widget.bindtags())
            for child in widget.winfo_children():
                bind_parameter_touchpad(child)

        bind_parameter_touchpad(parent)

    def _build_gcode_tab(self) -> None:
        top = ttk.Frame(self.gcode_tab)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(
            top,
            textvariable=self.gcode_summary_var,
            anchor="w",
            wraplength=760,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(
            top, text="Validate edits and refresh", command=self.validate_editor
        ).pack(side=tk.RIGHT)
        ttk.Button(top, text="Save G-code as", command=self.save_gcode_as).pack(
            side=tk.RIGHT, padx=6
        )
        editor_frame = ttk.Frame(self.gcode_tab)
        editor_frame.grid(row=1, column=0, sticky="nsew")
        self.gcode_text = tk.Text(
            editor_frame, wrap="none", undo=True, font=("Consolas", 10), tabs=(40,)
        )
        y_scroll = ttk.Scrollbar(
            editor_frame, orient=tk.VERTICAL, command=self.gcode_text.yview
        )
        x_scroll = ttk.Scrollbar(
            editor_frame, orient=tk.HORIZONTAL, command=self.gcode_text.xview
        )
        self.gcode_text.configure(
            yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set
        )
        self.gcode_text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        editor_frame.rowconfigure(0, weight=1)
        editor_frame.columnconfigure(0, weight=1)
        self.gcode_text.bind("<<Modified>>", self._on_editor_modified)
        review = ttk.LabelFrame(
            self.gcode_tab, text="Manual approval before sending", padding=8
        )
        review.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(
            review,
            variable=self.review_var,
            text="I checked units, range, speed, power, M5 and duration",
        ).pack(side=tk.LEFT)
        self.review_state_label = ttk.Label(
            review, text="Not validated", style="Fail.TLabel"
        )
        self.review_state_label.pack(side=tk.RIGHT)
        self.gcode_issue_text = tk.Text(
            review, height=4, wrap="word", font=("Consolas", 9), state=tk.DISABLED
        )
        self.gcode_issue_text.pack(fill=tk.X, pady=(6, 0))
        self.gcode_tab.rowconfigure(1, weight=1)
        self.gcode_tab.columnconfigure(0, weight=1)

    def _build_simulation_tab(self) -> None:
        ttk.Label(
            self.simulation_tab,
            textvariable=self.simulation_summary_var,
            wraplength=1250,
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(0, 4))
        live = ttk.Frame(self.simulation_tab)
        live.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(live, textvariable=self.accepted_line_var).pack(side=tk.LEFT)
        ttk.Label(live, textvariable=self.executed_line_var).pack(side=tk.LEFT, padx=16)
        ttk.Label(
            live, textvariable=self.laser_state_var, font=("Segoe UI", 10, "bold")
        ).pack(side=tk.LEFT, padx=16)
        ttk.Label(live, textvariable=self.status_rate_var).pack(side=tk.RIGHT)
        ttk.Label(
            self.simulation_tab,
            textvariable=self.execution_command_var,
            anchor="w",
            font=("Consolas", 9),
        ).pack(fill=tk.X, pady=(0, 4))
        self.figure = Figure(figsize=(8, 7), dpi=100)
        self.axis = self.figure.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.figure, master=self.simulation_tab)
        self.canvas.draw()
        toolbar = NavigationToolbar(
            self.canvas, self.simulation_tab, pack_toolbar=False
        )
        toolbar.update()
        toolbar.pack(fill=tk.X)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self._draw_empty_preview()

    def _build_device_tab(self) -> None:
        connection = ttk.LabelFrame(
            self.device_tab, text="FluidNC serial port", padding=8
        )
        connection.pack(fill=tk.X)
        ttk.Button(connection, text="Refresh ports", command=self.refresh_ports).grid(
            row=0, column=0, padx=(0, 5)
        )
        self.port_combo = ttk.Combobox(
            connection, textvariable=self.port_var, state="readonly", width=40
        )
        self.port_combo.grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Label(connection, text="Baud").grid(row=0, column=2, padx=(12, 3))
        ttk.Entry(connection, textvariable=self.baud_var, width=9).grid(row=0, column=3)
        self.connect_button = ttk.Button(
            connection, text="Connect", command=self.toggle_connection
        )
        self.connect_button.grid(row=0, column=4, padx=8)
        ttk.Button(
            connection,
            text="Query status ?",
            command=lambda: self.controller.query_status(),
        ).grid(row=1, column=0, padx=(0, 5), pady=(6, 0), sticky="w")
        ttk.Button(connection, text="Unlock $X", command=self.unlock_machine).grid(
            row=1, column=1, padx=5, pady=(6, 0), sticky="w"
        )
        connection.columnconfigure(1, weight=1)
        state = ttk.Frame(connection)
        state.grid(row=2, column=0, columnspan=7, sticky="ew", pady=(8, 0))
        ttk.Label(state, text="State:").pack(side=tk.LEFT)
        ttk.Label(
            state, textvariable=self.machine_state_var, font=("Segoe UI", 11, "bold")
        ).pack(side=tk.LEFT)
        ttk.Label(state, text="Work position:").pack(side=tk.LEFT, padx=(20, 3))
        ttk.Label(
            state, textvariable=self.position_var, font=("Consolas", 11, "bold")
        ).pack(side=tk.LEFT)
        ttk.Label(state, textvariable=self.laser_state_var).pack(
            side=tk.LEFT, padx=(30, 3)
        )
        live_state = ttk.Frame(connection)
        live_state.grid(row=3, column=0, columnspan=7, sticky="ew", pady=(4, 0))
        ttk.Label(live_state, textvariable=self.executed_line_var).pack(side=tk.LEFT)
        ttk.Label(live_state, textvariable=self.status_rate_var).pack(side=tk.RIGHT)
        ttk.Label(self.device_tab, text="Drag the divider to resize the console.").pack(
            anchor="w"
        )
        self.device_split = tk.PanedWindow(
            self.device_tab,
            orient=tk.HORIZONTAL,
            sashwidth=10,
            sashrelief=tk.RAISED,
            showhandle=True,
            opaqueresize=True,
        )
        self.device_split.pack(fill=tk.BOTH, expand=True, pady=6)
        left_panel = ttk.Frame(self.device_split)
        self.device_split.add(left_panel, minsize=480, stretch="always")
        control_canvas = tk.Canvas(left_panel, highlightthickness=0, width=900)
        control_scroll = ttk.Scrollbar(
            left_panel, orient=tk.VERTICAL, command=control_canvas.yview
        )
        control_xscroll = ttk.Scrollbar(
            left_panel, orient=tk.HORIZONTAL, command=control_canvas.xview
        )
        left_panel.rowconfigure(0, weight=1)
        left_panel.columnconfigure(0, weight=1)
        control_canvas.grid(row=0, column=0, sticky="nsew")
        control_scroll.grid(row=0, column=1, sticky="ns")
        control_xscroll.grid(row=1, column=0, sticky="ew")
        control_canvas.configure(
            yscrollcommand=control_scroll.set, xscrollcommand=control_xscroll.set
        )
        controls = ttk.Frame(control_canvas)
        control_window = control_canvas.create_window(
            (0, 0), window=controls, anchor="nw"
        )

        def resize_controls(_event=None):
            control_canvas.itemconfigure(
                control_window,
                width=max(control_canvas.winfo_width(), controls.winfo_reqwidth()),
            )
            control_canvas.configure(scrollregion=control_canvas.bbox("all"))

        controls.bind("<Configure>", resize_controls)
        control_canvas.bind("<Configure>", resize_controls)
        jog_frame = ttk.LabelFrame(controls, text="Manual XY jog", padding=8)
        send_frame = ttk.LabelFrame(controls, text="G-code sender", padding=8)
        jog_frame.pack(fill=tk.X, pady=(0, 6))
        send_frame.pack(fill=tk.X)
        self.manual_controls_frame = jog_frame
        jog_frame.columnconfigure(3, weight=1)
        stop_area = ttk.Frame(jog_frame, width=170)
        stop_area.grid(row=0, column=3, rowspan=4, sticky="nsew", padx=(10, 4), pady=4)
        self.manual_force_stop_button = tk.Button(
            stop_area,
            text="FORCE STOP",
            command=self.force_stop,
            font=("Segoe UI", 14, "bold"),
            background="#b91c1c",
            foreground="white",
            activebackground="#7f1d1d",
            activeforeground="white",
            relief=tk.RAISED,
            borderwidth=4,
            padx=8,
            pady=18,
            cursor="hand2",
            takefocus=True,
        )
        self.manual_force_stop_button.place(
            relx=0.5, rely=0.5, relwidth=1, relheight=0.75, anchor="center"
        )
        pad = ttk.Frame(jog_frame)
        pad.grid(row=0, column=0, rowspan=4, padx=(0, 14))
        ttk.Button(pad, text="Y+", width=8, command=lambda: self.jog("Y", +1)).grid(
            row=0, column=1, pady=3
        )
        ttk.Button(pad, text="X−", width=8, command=lambda: self.jog("X", -1)).grid(
            row=1, column=0, padx=3
        )
        ttk.Button(pad, text="Pause", width=11, command=self.pause_program).grid(
            row=1, column=1, padx=3
        )
        ttk.Button(pad, text="Resume", width=11, command=self.resume_program).grid(
            row=3, column=1, pady=3
        )
        ttk.Button(pad, text="X+", width=8, command=lambda: self.jog("X", +1)).grid(
            row=1, column=2, padx=3
        )
        ttk.Button(pad, text="Y−", width=8, command=lambda: self.jog("Y", -1)).grid(
            row=2, column=1, pady=3
        )
        ttk.Style(jog_frame).configure(
            "ManualMove.TButton", font=("Segoe UI", 10, "bold"), padding=(6, 6)
        )
        for button in pad.winfo_children():
            button.configure(style="ManualMove.TButton", width=12)
            button.grid_configure(padx=4, pady=4)
        ttk.Label(jog_frame, text="Step/mm").grid(row=0, column=1, sticky="e", pady=3)
        ttk.Entry(jog_frame, textvariable=self.param_vars["jog_step"], width=12).grid(
            row=0, column=2, pady=3
        )
        ttk.Label(jog_frame, text="Feed/mm/min").grid(
            row=1, column=1, sticky="e", pady=3
        )
        ttk.Entry(jog_frame, textvariable=self.param_vars["jog_feed"], width=12).grid(
            row=1, column=2, pady=3
        )
        ttk.Button(
            jog_frame, text="Set current XY as zero", command=self.set_work_zero
        ).grid(row=2, column=1, columnspan=2, sticky="ew", pady=3)
        home_buttons = ttk.Frame(jog_frame)
        home_buttons.grid(row=3, column=1, columnspan=2, sticky="ew", pady=3)
        for axis in ("X", "Y"):
            ttk.Button(
                home_buttons,
                text=f"Home {axis} $H{axis}",
                command=lambda a=axis: self.home_machine(a),
            ).pack(side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Label(
            jog_frame,
            text="One click = one move. Pause/Resume keeps remaining travel. Laser off during manual moves.",
            wraplength=600,
        ).grid(row=4, column=0, columnspan=3, sticky="w")
        self.control_lock_label = ttk.Label(
            jog_frame,
            text="While running: new commands are blocked, not queued. Pause, Resume and Force Stop remain available.",
            foreground="#b91c1c",
            wraplength=850,
        )
        self.control_lock_label.grid(
            row=5, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )
        self.send_button = ttk.Button(
            send_frame, text="Send reviewed G-code", command=self.send_program
        )
        self.send_button.grid(row=0, column=0, padx=(0, 6), pady=3)
        self.pause_button = ttk.Button(
            send_frame, text="Pause !", command=self.pause_program
        )
        self.pause_button.grid(row=0, column=1, padx=6, pady=3)
        self.resume_button = ttk.Button(
            send_frame, text="Resume ~", command=self.resume_program
        )
        self.resume_button.grid(row=0, column=2, padx=6, pady=3)
        ttk.Button(
            send_frame,
            text="FORCE STOP",
            style="Emergency.TButton",
            command=self.force_stop,
        ).grid(row=0, column=3, padx=6, pady=3)
        self.progress = ttk.Progressbar(
            send_frame, variable=self.progress_var, maximum=100.0
        )
        self.progress.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(10, 3))
        ttk.Label(send_frame, textvariable=self.progress_text_var).grid(
            row=2, column=0, columnspan=4, sticky="w"
        )
        ttk.Label(
            send_frame,
            text="Software stop is not a physical E-stop.",
            foreground="#b00020",
            wraplength=1050,
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        send_frame.columnconfigure(3, weight=1)
        recalibration = ttk.Frame(send_frame)
        recalibration.grid(row=4, column=0, columnspan=4, sticky="ew", pady=4)
        self.x_home_check = ttk.Checkbutton(
            recalibration, text="Periodic X homing $HX", variable=self.x_home_enabled
        )
        self.x_home_check.pack(side=tk.LEFT)
        ttk.Label(recalibration, text="Scan rows per home").pack(side=tk.LEFT, padx=8)
        self.x_home_entry = ttk.Spinbox(
            recalibration,
            from_=1,
            to=100000,
            textvariable=self.x_home_interval,
            width=7,
        )
        self.x_home_entry.pack(side=tk.LEFT)
        ttk.Label(
            send_frame,
            text="Homes X before the job and at the selected interval. Resets X0; keeps Y.",
            wraplength=1000,
        ).grid(row=5, column=0, columnspan=4, sticky="w")
        console_frame = ttk.LabelFrame(
            self.device_split, text="FluidNC Console", padding=4
        )
        self.device_split.add(console_frame, minsize=240, width=340, stretch="never")
        self.console_text = tk.Text(
            console_frame,
            wrap="char",
            width=32,
            height=10,
            font=("Consolas", 9),
            state=tk.DISABLED,
        )
        console_scroll = ttk.Scrollbar(
            console_frame, orient=tk.VERTICAL, command=self.console_text.yview
        )
        self.console_text.configure(yscrollcommand=console_scroll.set)
        self.console_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        console_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _ui(self, callback, *args) -> None:
        try:
            self.root.after(0, callback, *args)
        except tk.TclError:
            pass

    def _float(self, key: str, label: str, *, positive: bool = True) -> float:
        english_label = _PARAMETER_ENGLISH.get(key, label)
        try:
            value = float(self.param_vars[key].get())
        except ValueError as exc:
            raise ValueError(f"{english_label} must be numeric.") from exc
        if not math.isfinite(value):
            raise ValueError("Parameter must be finite.")
        if key == "jog_feed" and (not 0.001 <= value <= 100):
            raise ValueError(f"{english_label} must be in [0.001, 100].")
        if key in {"feed", "acceleration"} and (not 0.001 <= value <= 10):
            raise ValueError(f"{english_label} must be in [0.001, 10].")
        if positive and value <= 0:
            raise ValueError(f"{english_label} must be greater than 0.")
        return value

    def _int(self, key: str, label: str, *, minimum: int = 0) -> int:
        english_label = _PARAMETER_ENGLISH.get(key, label)
        try:
            value = int(self.param_vars[key].get())
        except ValueError as exc:
            raise ValueError(f"{english_label} must be an integer.") from exc
        if value < minimum:
            raise ValueError(f"{english_label} cannot be less than {minimum}.")
        return value

    def _rules(self) -> DesignRules:
        return DesignRules(
            max_width_mm=self._float("max_width", "Maximum width"),
            max_height_mm=self._float("max_height", "Maximum height"),
            min_feature_mm=self._float("min_feature_um", "Minimum feature") / 1000.0,
            min_spacing_mm=self._float("min_spacing_um", "Minimum spacing") / 1000.0,
            target_precision_mm=self._float("precision_um", "Target precision")
            / 1000.0,
            hatch_spacing_mm=self._float("hatch_um", "Hatch spacing") / 1000.0,
            x_steps_per_mm=self._float("x_steps", "X steps/mm"),
            y_steps_per_mm=self._float("y_steps", "Y steps/mm"),
        )

    def _path_settings(self) -> PathSettings:
        return PathSettings(
            hatch_spacing_mm=self._float("hatch_um", "Hatch spacing") / 1000.0,
            laser_spot_mm=self._float("spot_um", "Preview spot") / 1000.0,
            feed_mm_min=self._float("feed", "Feed"),
            acceleration_mm_s2=self._float("acceleration", "Acceleration"),
            laser_power=self._int("power", "Laser output"),
            max_laser_power=self._int("max_power", "Maximum laser output", minimum=1),
            overscan_mm=self._float("overscan", "overscan", positive=False),
            row_settle_ms=self._int("settle_ms", "Row dwell"),
            x_steps_per_mm=self._float("x_steps", "X steps/mm"),
            y_steps_per_mm=self._float("y_steps", "Y steps/mm"),
        )

    def _generation_signature(self) -> tuple[float | int, ...]:
        rules = self._rules()
        settings = self._path_settings()
        return (
            rules.max_width_mm,
            rules.max_height_mm,
            rules.min_feature_mm,
            rules.min_spacing_mm,
            rules.target_precision_mm,
            rules.hatch_spacing_mm,
            rules.x_steps_per_mm,
            rules.y_steps_per_mm,
            settings.laser_spot_mm,
            settings.feed_mm_min,
            settings.acceleration_mm_s2,
            settings.laser_power,
            settings.max_laser_power,
            settings.overscan_mm,
            settings.row_settle_ms,
            self._float("machine_x", "X travel"),
            self._float("machine_y", "Y travel"),
        )

    def _on_generation_parameter_changed(self, *_args) -> None:
        if self.generated_program is None:
            return
        self.review_var.set(False)
        self.review_state_label.configure(
            text="Parameters changed; regenerate", style="Fail.TLabel"
        )
        self.status_var.set(
            "Generation parameters changed; rerun the design check and generate G-code again."
        )

    def open_gds(self) -> None:
        filename = filedialog.askopenfilename(
            title="Select GDS file",
            filetypes=[("GDS files", "*.gds *.gdsii"), ("All files", "*.*")],
        )
        if not filename:
            return
        try:
            document = load_gds(filename)
        except Exception as exc:
            messagebox.showerror("Cannot open GDS", str(exc), parent=self.root)
            return
        self.document = document
        self.file_var.set(str(document.path))
        names = list(document.cells)
        self.cell_combo["values"] = names
        self.cell_var.set(names[0])
        self.generated_program = None
        self.generation_signature = None
        self.validated_digest = None
        self.review_var.set(False)
        self.status_var.set(
            f"Loaded {document.path.name}; GDS unit={document.library.unit:g} m, scale={document.scale_to_mm:g} mm/unit."
        )
        self.refresh_layers()

    def refresh_layers(self) -> None:
        self.layer_tree.delete(*self.layer_tree.get_children())
        self.layer_infos.clear()
        self.selected_layer = None
        if not self.document or not self.cell_var.get():
            return
        try:
            infos = list_layers(self.document, self.cell_var.get())
            max_width = self._float("max_width", "Maximum width")
            max_height = self._float("max_height", "Maximum height")
        except Exception as exc:
            messagebox.showerror("Layer read failed", str(exc), parent=self.root)
            return
        for info in infos:
            self.layer_infos[info.key] = info
            size_ok = info.width_mm <= max_width and info.height_mm <= max_height
            iid = f"{info.layer}/{info.datatype}"
            self.layer_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    info.layer,
                    info.datatype,
                    info.polygon_count,
                    f"{info.width_mm:.4f}",
                    f"{info.height_mm:.4f}",
                    "PASS" if size_ok else "FAIL",
                ),
            )
        self.status_var.set(
            f"Detected {len(infos)} Layer/Datatype entries; select the exposure layer."
        )

    def _on_layer_selected(self, _event=None) -> None:
        selection = self.layer_tree.selection()
        if not selection:
            self.selected_layer = None
            return
        layer, datatype = selection[0].split("/")
        self.selected_layer = (int(layer), int(datatype))
        self.last_drc = None
        self.status_var.set(f"Selected Layer/Datatype {layer}/{datatype}.")

    def _selected_info(self) -> LayerInfo:
        if not self.document:
            raise ValueError("Open a GDS file first.")
        if self.selected_layer is None:
            raise ValueError("Select a Layer/Datatype first.")
        return self.layer_infos[self.selected_layer]

    def _selected_polygons(self, *, shift: bool) -> list[Polygon]:
        info = self._selected_info()
        assert self.document is not None
        return polygons_for_layer(
            self.document,
            self.cell_var.get(),
            info.layer,
            info.datatype,
            shift_to_origin=shift,
        )

    def run_drc_action(self, *, show_dialog: bool = True) -> bool:
        try:
            result = run_drc(self._selected_polygons(shift=False), self._rules())
        except Exception as exc:
            if show_dialog:
                show_check_failure(self.root, "Check failed", str(exc))
            self.status_var.set(f"Check failed: {exc}")
            return False
        self.last_drc = result
        self._set_text(self.drc_text, result.report_text())
        if result.passed:
            self.status_var.set("Design checks passed.")
            if show_dialog:
                messagebox.showinfo(
                    "Check passed", result.report_text(), parent=self.root
                )
        else:
            self.status_var.set("Design check failed; G-code generation is blocked.")
            if show_dialog:
                show_check_failure(self.root, "Check not passed", result.report_text())
        return result.passed

    def generate_action(self) -> None:
        if not self.run_drc_action(show_dialog=False):
            show_check_failure(
                self.root,
                "Cannot generate",
                "The design check failed. Review the results and correct the settings or GDS.",
            )
            return
        try:
            info = self._selected_info()
            assert self.document is not None
            settings = self._path_settings()
            shifted_polygons = self._selected_polygons(shift=True)
            geometry = unary_union(shifted_polygons)
            plan = generate_serpentine_plan(geometry, settings)
            machine_x = self._float("machine_x", "X travel")
            machine_y = self._float("machine_y", "Y travel")
            program = build_gcode(
                plan,
                settings,
                source_path=self.document.path,
                cell_name=self.cell_var.get(),
                layer_info=info,
                machine_width_mm=machine_x,
                machine_height_mm=machine_y,
            )
            stem = f"{self.document.path.stem}_L{info.layer}_D{info.datatype}_safe_serpentine"
            output_path = self.document.path.parent / f"{stem}.gcode"
            simulation_path = self.document.path.parent / f"{stem}_simulation.png"
            output_path.write_text(program.text, encoding="utf-8")
            program.output_path = output_path
            program.simulation_path = simulation_path
            save_simulation_png(program, simulation_path, settings.laser_spot_mm)
        except Exception as exc:
            messagebox.showerror("Generation failed", str(exc), parent=self.root)
            self.status_var.set(f"Generation failed: {exc}")
            return
        self.current_geometry = geometry
        self.generated_program = program
        self.generation_signature = self._generation_signature()
        self._load_editor(program.text)
        self.validate_editor(show_dialog=False)
        self.review_var.set(False)
        self.status_var.set(
            f"Generated {output_path.name}; review it manually before sending."
        )
        self.notebook.select(self.gcode_tab)
        messagebox.showinfo(
            "Generated",
            f"{'G-code file:'}\n{output_path}\n\n{'Simulation image:'}\n{simulation_path}\n\n{'Scan rows:'} {len(plan.rows)}\n{program.analysis.summary_text()}\n\n"
            + "Next: manually review the G-code, then tick the approval box.",
            parent=self.root,
        )

    def _load_editor(self, text: str) -> None:
        self.original_generated_digest = text_digest(text.rstrip("\n"))
        self._setting_editor_text = True
        self.gcode_text.delete("1.0", tk.END)
        self.gcode_text.insert("1.0", text)
        self.gcode_text.edit_modified(False)
        self._setting_editor_text = False

    def _on_editor_modified(self, _event=None) -> None:
        if self._setting_editor_text:
            self.gcode_text.edit_modified(False)
            return
        if self.gcode_text.edit_modified():
            self.validated_digest = None
            self.review_var.set(False)
            self.review_state_label.configure(
                text="Edits not validated", style="Fail.TLabel"
            )
            self.gcode_text.edit_modified(False)

    def validate_editor(self, *, show_dialog: bool = True) -> bool:
        text = self.gcode_text.get("1.0", "end-1c")
        try:
            settings = self._path_settings()
            analysis = analyze_gcode(
                text,
                machine_width_mm=self._float("machine_x", "X travel"),
                machine_height_mm=self._float("machine_y", "Y travel"),
                max_feed_mm_min=settings.feed_mm_min,
                max_laser_power=settings.max_laser_power,
            )
        except Exception as exc:
            show_check_failure(self.root, "Validation setting error", str(exc))
            return False
        issue_lines = [f"{'Error'}：{item}" for item in analysis.errors]
        issue_lines.extend((f"{'Warning'}：{item}" for item in analysis.warnings))
        if not issue_lines:
            issue_lines.append(
                "No syntax, range, speed or laser-off issue found. The physical origin and wiring still require manual verification."
            )
        self._set_text(self.gcode_issue_text, "\n".join(issue_lines))
        self.gcode_summary_var.set(analysis.summary_text())
        if not analysis.valid:
            self.validated_digest = None
            self.review_var.set(False)
            self.review_state_label.configure(
                text="Validation failed", style="Fail.TLabel"
            )
            if show_dialog:
                show_check_failure(
                    self.root, "G-code validation failed", "\n".join(analysis.errors)
                )
            return False
        previous = self.generated_program
        self.generated_program = GCodeProgram(
            lines=text.replace("\r\n", "\n").replace("\r", "\n").split("\n"),
            analysis=analysis,
            path_plan=previous.path_plan if previous else None,
            output_path=previous.output_path if previous else None,
            simulation_path=previous.simulation_path if previous else None,
        )
        self.validated_digest = text_digest(text)
        self.review_var.set(False)
        self.review_state_label.configure(
            text="Machine check passed; awaiting approval", style="Pass.TLabel"
        )
        self._draw_preview(self.generated_program)
        self.status_var.set(
            "G-code machine check passed; complete the manual review and tick approval."
        )
        if show_dialog:
            warning_text = "\n".join(analysis.warnings) if analysis.warnings else "None"
            messagebox.showinfo(
                "Validation passed",
                f"{analysis.summary_text()}\n\n{'Warnings:'}\n{warning_text}",
                parent=self.root,
            )
        return True

    def save_gcode_as(self) -> None:
        text = self.gcode_text.get("1.0", "end-1c")
        if not text.strip():
            messagebox.showwarning(
                "No content", "There is no G-code.", parent=self.root
            )
            return
        initial = (
            self.generated_program.output_path.name
            if self.generated_program and self.generated_program.output_path
            else "output.gcode"
        )
        filename = filedialog.asksaveasfilename(
            title="Save G-code",
            defaultextension=".gcode",
            initialfile=initial,
            filetypes=[("G-code", "*.gcode *.nc *.tap"), ("All files", "*.*")],
        )
        if not filename:
            return
        try:
            Path(filename).write_text(text.rstrip() + "\n", encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc), parent=self.root)
            return
        self.status_var.set(f"Saved {filename}")

    def _draw_empty_preview(self) -> None:
        self.axis.clear()
        self.axis.set_title("No G-code generated")
        self.axis.set_xlabel("X / mm")
        self.axis.set_ylabel("Y / mm")
        self.axis.grid(True, linewidth=0.3, alpha=0.4)
        self.canvas.draw_idle()

    def _draw_preview(
        self, program: GCodeProgram, current_line: int | None = None
    ) -> None:
        self.axis.clear()
        travel = []
        burn = []
        burn_colors = []
        max_power = max(
            (segment.power for segment in program.analysis.segments), default=1.0
        )
        for segment in program.analysis.segments:
            points = [
                (segment.start_x_mm, segment.start_y_mm),
                (segment.end_x_mm, segment.end_y_mm),
            ]
            if segment.laser_on:
                burn.append(points)
                alpha = max(0.3, min(1.0, segment.power / max_power))
                burn_colors.append((0.88, 0.05, 0.05, alpha))
            else:
                travel.append(points)
        if travel:
            self.axis.add_collection(
                LineCollection(
                    travel,
                    colors="#4f86b5",
                    linewidths=0.45,
                    alpha=0.35,
                    label="Laser-off travel",
                )
            )
        if burn:
            self.axis.add_collection(
                LineCollection(
                    burn,
                    colors=burn_colors,
                    linewidths=0.8,
                    alpha=0.95,
                    label="Exposure path",
                )
            )
        if self.current_geometry is not None and program.path_plan is not None:
            shifted = affinity.translate(
                self.current_geometry,
                xoff=program.path_plan.design_x_offset_mm,
                yoff=0.0,
            )
            self._plot_geometry_outline(shifted)
        self.axis.autoscale()
        self.axis.margins(0.03)
        self.axis.set_aspect("equal", adjustable="box")
        self.axis.set_xlabel("X / mm")
        self.axis.set_ylabel("Y / mm")
        self.axis.set_title("Planned path (not measured motion)")
        self.axis.grid(True, linewidth=0.3, alpha=0.4)
        (self._accepted_artist,) = self.axis.plot(
            [],
            [],
            linestyle="none",
            marker="s",
            markersize=8,
            markerfacecolor="none",
            markeredgewidth=1.5,
            markeredgecolor="#ff8c00",
            label="Latest accepted line",
        )
        (self._reported_artist,) = self.axis.plot(
            [],
            [],
            linestyle="none",
            marker="o",
            markersize=8,
            markerfacecolor="#ffd600",
            markeredgecolor="#222222",
            label="FluidNC reported position",
        )
        self.axis.legend(loc="upper right", fontsize=8)
        self._segment_line_indices = [
            segment.line_index for segment in program.analysis.segments
        ]
        self._accepted_line_index = -1
        self._executed_segment_cursor = 0
        self.accepted_line_var.set("FluidNC accepted line: —")
        self.executed_line_var.set("Estimated active line: —")
        if current_line is not None:
            self._update_accepted_position(current_line)
        self.figure.tight_layout()
        self.canvas.draw_idle()
        self.simulation_summary_var.set(
            program.analysis.summary_text()
            + "｜"
            + "red=exposure, blue=off, orange=accepted, dot=FluidNC position"
        )

    def _plot_geometry_outline(self, geometry) -> None:
        if geometry.is_empty:
            return
        if isinstance(geometry, Polygon):
            x, y = geometry.exterior.xy
            self.axis.plot(x, y, color="#222222", linewidth=0.35, alpha=0.45)
            for ring in geometry.interiors:
                hx, hy = ring.xy
                self.axis.plot(hx, hy, color="#222222", linewidth=0.3, alpha=0.35)
        elif isinstance(geometry, (MultiPolygon, GeometryCollection)):
            for part in geometry.geoms:
                self._plot_geometry_outline(part)

    def _update_accepted_position(self, line_index: int) -> None:
        if (
            not self.generated_program
            or not self._accepted_artist
            or (not self._segment_line_indices)
        ):
            return
        index = bisect.bisect_right(self._segment_line_indices, line_index) - 1
        if index < 0:
            return
        segment = self.generated_program.analysis.segments[index]
        self._accepted_artist.set_data([segment.end_x_mm], [segment.end_y_mm])
        self._request_live_canvas_draw()

    def _update_reported_position(
        self, x_mm: float, y_mm: float, laser_on: bool, power: float
    ) -> None:
        if not self._reported_artist:
            return
        self._reported_artist.set_data([x_mm], [y_mm])
        self._reported_artist.set_markerfacecolor("#e00020" if laser_on else "#ffd600")
        self._reported_artist.set_markersize(10 if laser_on else 8)
        self._request_live_canvas_draw()

    def _request_live_canvas_draw(self) -> None:
        if self.notebook.select() == str(self.simulation_tab):
            self.canvas.draw_idle()

    def _on_tab_changed(self, _event=None) -> None:
        if hasattr(self, "canvas") and self.notebook.select() == str(
            self.simulation_tab
        ):
            self.canvas.draw_idle()

    @staticmethod
    def _point_segment_distance_sq(x: float, y: float, segment) -> float:
        dx = segment.end_x_mm - segment.start_x_mm
        dy = segment.end_y_mm - segment.start_y_mm
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-16:
            return (x - segment.start_x_mm) ** 2 + (y - segment.start_y_mm) ** 2
        ratio = (
            (x - segment.start_x_mm) * dx + (y - segment.start_y_mm) * dy
        ) / length_sq
        ratio = max(0.0, min(1.0, ratio))
        nearest_x = segment.start_x_mm + ratio * dx
        nearest_y = segment.start_y_mm + ratio * dy
        return (x - nearest_x) ** 2 + (y - nearest_y) ** 2

    def _estimate_execution_line(self, x_mm: float, y_mm: float) -> int | None:
        if (
            not self.generated_program
            or self._accepted_line_index < 0
            or (not self._segment_line_indices)
        ):
            return None
        segments = self.generated_program.analysis.segments
        accepted_segment = (
            bisect.bisect_right(self._segment_line_indices, self._accepted_line_index)
            - 1
        )
        if accepted_segment < 0:
            return None
        search_start = max(0, self._executed_segment_cursor - 3, accepted_segment - 192)
        search_end = min(len(segments) - 1, accepted_segment)
        best_index = search_start
        best_distance = math.inf
        for index in range(search_start, search_end + 1):
            distance = self._point_segment_distance_sq(x_mm, y_mm, segments[index])
            if distance <= best_distance + 1e-14:
                best_distance = distance
                best_index = index
        self._executed_segment_cursor = max(self._executed_segment_cursor, best_index)
        return segments[self._executed_segment_cursor].line_index

    def refresh_ports(self) -> None:
        ports = available_ports()
        self._ports = {item.display_name: item for item in ports}
        names = list(self._ports)
        self.port_combo["values"] = names
        if names:
            if self.port_var.get() not in self._ports:
                self.port_var.set(names[0])
            self.status_var.set(f"Detected {len(names)} serial device(s).")
        else:
            self.port_var.set("")
            self.status_var.set("No serial device detected.")

    def open_pd_window(self) -> None:
        if self.pd_window is not None:
            self.pd_window.focus()
            return
        self.pd_window = PDMonitorWindow(
            self.root,
            reserved_port_provider=lambda: self.controller.port,
            fluidnc_busy_provider=lambda: self.controller.streaming,
            on_closed=self._pd_window_closed,
        )

    def _pd_window_closed(self) -> None:
        self.pd_window = None

    def toggle_connection(self) -> None:
        if self.controller.connected:
            self.controller.disconnect()
            return
        selected = self._ports.get(self.port_var.get())
        if not selected:
            messagebox.showwarning(
                "No port selected",
                "Select the serial port for the FluidNC ESP32.",
                parent=self.root,
            )
            return
        if self.pd_window and self.pd_window.connected_port == selected.device:
            messagebox.showerror(
                "Port conflict",
                "This port is being used by the independent PD monitor.",
                parent=self.root,
            )
            return
        try:
            baud = int(self.baud_var.get())
            self.controller.connect(selected.device, baud)
        except Exception as exc:
            show_connection_failure(self.root, str(exc))

    def _handle_connection(self, connected: bool, text: str) -> None:
        self.connection_var.set(text)
        self.connect_button.configure(text="Disconnect" if connected else "Connect")
        if not connected:
            self.machine_state_var.set("Disconnected")
            self._last_reported_power = 0.0
            self._last_wco = None
            self.laser_state_var.set("Laser command: unknown")
            self.status_rate_var.set("Status refresh: 0 Hz")
            self._status_timestamps.clear()
        self.status_var.set(text)

    def _handle_serial_line(self, line: str) -> None:
        if line.startswith("<") and line.endswith(">"):
            self._parse_status(line)
            return
        self._append_console(line)

    def _parse_status(self, line: str) -> None:
        body = line[1:-1]
        parts = body.split("|")
        state = parts[0] if parts else "Unknown"
        self.machine_state_var.set(state)
        fields: dict[str, str] = {}
        for part in parts[1:]:
            if ":" in part:
                key, value = part.split(":", 1)
                fields[key] = value
        now = time.monotonic()
        self._status_timestamps.append(now)
        if len(self._status_timestamps) >= 2:
            elapsed = self._status_timestamps[-1] - self._status_timestamps[0]
            rate = (len(self._status_timestamps) - 1) / elapsed if elapsed > 0 else 0.0
            self.status_rate_var.set(f"Status refresh: {rate:.1f} Hz")

        def xy_from(field_name: str) -> tuple[float, float] | None:
            value = fields.get(field_name)
            if not value:
                return None
            try:
                numbers = [float(item) for item in value.split(",")]
            except ValueError:
                return None
            return (numbers[0], numbers[1]) if len(numbers) >= 2 else None

        wco = xy_from("WCO")
        if wco is not None:
            self._last_wco = wco
        work_position = xy_from("WPos")
        machine_position = xy_from("MPos")
        if (
            work_position is None
            and machine_position is not None
            and (self._last_wco is not None)
        ):
            work_position = (
                machine_position[0] - self._last_wco[0],
                machine_position[1] - self._last_wco[1],
            )
        display_position = work_position or machine_position
        if display_position is not None:
            prefix = "W" if work_position is not None else "M"
            self.position_var.set(
                f"{prefix} X {display_position[0]:.4f}   Y {display_position[1]:.4f}"
            )
        power = self._last_reported_power
        power_was_reported = False
        fs_value = fields.get("FS")
        if fs_value:
            try:
                fs_numbers = [float(item) for item in fs_value.split(",")]
                if len(fs_numbers) >= 2:
                    power = fs_numbers[1]
                    power_was_reported = True
            except ValueError:
                pass
        elif "S" in fields:
            try:
                power = float(fields["S"])
                power_was_reported = True
            except ValueError:
                pass
        if power_was_reported:
            self._last_reported_power = power
        laser_on = power > 0.0
        self.laser_state_var.set(
            f"Laser command: {('ON' if laser_on else 'OFF')} S{power:g}"
        )
        if work_position is not None:
            self._update_reported_position(
                work_position[0], work_position[1], laser_on, power
            )
            estimated_line = self._estimate_execution_line(
                work_position[0], work_position[1]
            )
            if estimated_line is not None:
                self.executed_line_var.set(
                    f"Estimated active line: {estimated_line + 1}"
                )
                if self.generated_program and 0 <= estimated_line < len(
                    self.generated_program.lines
                ):
                    command = self.generated_program.lines[estimated_line].strip()
                    self.execution_command_var.set(
                        f"Estimated active command: {command}"
                    )

    def _append_console(self, text: str) -> None:
        self.console_text.configure(state=tk.NORMAL)
        self.console_text.insert(tk.END, text + "\n")
        line_count = int(self.console_text.index("end-1c").split(".")[0])
        if line_count > 2000:
            self.console_text.delete("1.0", "500.0")
        self.console_text.see(tk.END)
        self.console_text.configure(state=tk.DISABLED)

    def jog(self, axis: str, direction: int) -> None:
        if not self.controller.connected:
            show_connection_failure(self.root)
            return
        try:
            step = self._float("jog_step", "Jog distance")
            feed = self._float("jog_feed", "Jog feed")
            self.controller.jog(axis, direction * step, feed)
            self._sync_control_lock()
        except Exception as exc:
            messagebox.showerror("Jog failed", str(exc), parent=self.root)

    def set_work_zero(self) -> None:
        if not messagebox.askyesno(
            "Set work zero",
            "Confirm that the current laser-head position is the safe lower-left G-code origin?\nG10 L20 P0 X0 Y0 will be executed.",
            parent=self.root,
        ):
            return
        if not self.controller.connected:
            show_connection_failure(self.root)
            return
        try:
            self.controller.set_work_zero_xy()
        except Exception as exc:
            messagebox.showerror("Setting failed", str(exc), parent=self.root)

    def home_machine(self, axis: str) -> None:
        if self.controller.controls_locked:
            self._sync_control_lock()
            messagebox.showwarning(
                "Controls locked",
                "Homing is blocked during motion or pause; request rejected, not queued.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Run homing",
            f"Home only {axis} using its own switch ($H{axis}). Confirm firmware is 200 steps/mm (full step) and homing feed≤10 mm/min; UI values do not change firmware. Verify switch/direction. Stop if buzzing without motion. Continue?",
            parent=self.root,
        ):
            return
        if not self.controller.connected:
            show_connection_failure(self.root)
            return
        try:
            self.controller.home(axis)
            self._sync_control_lock()
        except Exception as exc:
            messagebox.showerror("Homing failed", str(exc), parent=self.root)

    def unlock_machine(self) -> None:
        if not self.controller.connected:
            show_connection_failure(self.root)
            return
        try:
            self.controller.unlock()
        except Exception as exc:
            messagebox.showerror("Unlock failed", str(exc), parent=self.root)

    def send_program(self) -> None:
        text = self.gcode_text.get("1.0", "end-1c")
        try:
            current_signature = self._generation_signature()
        except ValueError as exc:
            messagebox.showerror("Setting error", str(exc), parent=self.root)
            return
        if self.generation_signature != current_signature:
            messagebox.showerror(
                "Regeneration required",
                "Machine, design-rule or path settings changed after generation. Rerun the check and generate G-code again.",
                parent=self.root,
            )
            self.notebook.select(self.design_tab)
            return
        current_digest = text_digest(text)
        if self.validated_digest != current_digest:
            if not self.validate_editor(show_dialog=False):
                show_check_failure(
                    self.root, "Cannot send", "The G-code machine check did not pass."
                )
                return
            messagebox.showwarning(
                "Manual approval required again",
                "The G-code was just revalidated. Return to the review page, check it, and tick approval again.",
                parent=self.root,
            )
            self.notebook.select(self.gcode_tab)
            return
        if not self.review_var.get():
            messagebox.showwarning(
                "Manual review required",
                "Complete the manual G-code review and tick the approval box.",
                parent=self.root,
            )
            self.notebook.select(self.gcode_tab)
            return
        if not self.controller.connected:
            show_connection_failure(self.root)
            return
        assert self.generated_program is not None
        interval = 0
        if self.x_home_enabled.get():
            try:
                interval = int(self.x_home_interval.get())
                if interval < 1:
                    raise ValueError()
                if text_digest(text.rstrip("\n")) != self.original_generated_digest:
                    raise ValueError()
                if "; ROW_END " not in text:
                    raise ValueError()
            except ValueError:
                messagebox.showerror(
                    "X homing setting error",
                    "Use a positive row interval and freshly generated, unmodified G-code.",
                    parent=self.root,
                )
                return
        confirmation = (
            f"{'About to send:'}\n{self.generated_program.analysis.summary_text()}\n\n{'Confirm:'}\nCheck work zero, travel clearance and laser protection.\n\n"
            + (
                f"Initial $HX resets work X0, then X homes every {interval} scan rows with Y unchanged. Confirm X switch homing has been physically tested.\n"
                if interval
                else ""
            )
            + "Start now?"
        )
        if not messagebox.askyesno(
            "Final confirmation before sending", confirmation, parent=self.root
        ):
            return
        try:
            self.progress_var.set(0.0)
            self.progress_text_var.set("Preparing to send…")
            self.controller.send_program(text, x_home_interval=interval)
            self._sync_control_lock()
            self.x_home_check.configure(state="disabled")
            self.x_home_entry.configure(state="disabled")
        except Exception as exc:
            messagebox.showerror("Send failed", str(exc), parent=self.root)

    def pause_program(self) -> None:
        self.controller.pause()

    def resume_program(self) -> None:
        self.controller.resume()

    def force_stop(self) -> None:
        self.controller.force_stop()
        self.progress_text_var.set(
            "Force stop requested; observe FluidNC and machine state"
        )
        self.status_var.set(
            "Real-time hold, soft reset and M5 sent. A software button cannot replace a physical E-stop."
        )

    def _handle_progress(self, done: int, total: int, line_index: int) -> None:
        percentage = 100.0 * done / max(1, total)
        self.progress_var.set(percentage)
        self.progress_text_var.set(f"Acknowledged {done}/{total} ({percentage:.1f}%)")
        self._accepted_line_index = line_index
        self.accepted_line_var.set(f"FluidNC accepted line: {line_index + 1}")
        if done == total or done % 8 == 0:
            self._update_accepted_position(line_index)

    def _handle_complete(self, success: bool, text: str) -> None:
        self.x_home_check.configure(state="normal")
        self.x_home_entry.configure(state="normal")
        self.progress_text_var.set(text)
        self.status_var.set(text)
        if success:
            self.progress_var.set(100.0)
            messagebox.showinfo("Send complete", text, parent=self.root)
        else:
            messagebox.showwarning("Send ended", text, parent=self.root)

    def _sync_control_lock(self) -> None:
        if not hasattr(self, "controller"):
            return
        locked = self.controller.controls_locked

        def set_locked(widget):
            if widget.instate(["disabled"]) != locked:
                widget.state(["disabled"] if locked else ["!disabled"])

        def update_children(parent):
            for widget in parent.winfo_children():
                if isinstance(widget, ttk.Button):
                    label = str(widget.cget("text"))
                    if not any(
                        (word in label for word in ("Pause", "Resume", "FORCE STOP"))
                    ):
                        set_locked(widget)
                update_children(widget)

        update_children(self.manual_controls_frame)
        set_locked(self.send_button)
        set_locked(self.x_home_check)
        set_locked(self.x_home_entry)
        self.control_lock_label.configure(
            text=(
                "Busy: new commands disabled. Pause, Resume and Force Stop remain available."
                if locked
                else "Ready: one command at a time."
            )
        )

    def _poll_status(self) -> None:
        try:
            self._sync_control_lock()
            if hasattr(self, "controller") and self.controller.connected:
                self.controller.query_status()
            delay_ms = (
                100
                if hasattr(self, "controller") and self.controller.streaming
                else 250
            )
            self.root.after(delay_ms, self._poll_status)
        except tk.TclError:
            pass

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert("1.0", value)
        widget.configure(state=tk.DISABLED)

    def _on_close(self) -> None:
        if self.pd_window is not None:
            self.pd_window.close()
        if hasattr(self, "controller") and self.controller.connected:
            self.controller.force_stop()
            self.controller.disconnect()
        self.root.destroy()


def main() -> None:
    from laser_writer_launcher import main as launch

    launch()


if __name__ == "__main__":
    main()
