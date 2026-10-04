from __future__ import annotations
import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence
import gdstk
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
)
from shapely.ops import unary_union
from shapely.strtree import STRtree

APP_VERSION = "1.4.1"
EPSILON_MM = 1e-08


@dataclass(frozen=True)
class LayerInfo:
    layer: int
    datatype: int
    polygon_count: int
    min_x_mm: float
    min_y_mm: float
    max_x_mm: float
    max_y_mm: float

    @property
    def width_mm(self) -> float:
        return self.max_x_mm - self.min_x_mm

    @property
    def height_mm(self) -> float:
        return self.max_y_mm - self.min_y_mm

    @property
    def key(self) -> tuple[int, int]:
        return (self.layer, self.datatype)


@dataclass
class GdsDocument:
    path: Path
    library: gdstk.Library
    scale_to_mm: float
    cells: dict[str, gdstk.Cell]


@dataclass(frozen=True)
class DesignRules:
    max_width_mm: float = 15.0
    max_height_mm: float = 15.0
    min_feature_mm: float = 0.1
    min_spacing_mm: float = 0.1
    target_precision_mm: float = 0.1
    hatch_spacing_mm: float = 0.025
    x_steps_per_mm: float = 200.0
    y_steps_per_mm: float = 200.0


@dataclass(frozen=True)
class CheckItem:
    name: str
    measured: str
    requirement: str
    passed: bool
    detail: str = ""


@dataclass
class DrcResult:
    items: list[CheckItem]
    min_feature_mm: float
    min_spacing_mm: float | None

    @property
    def passed(self) -> bool:
        return all((item.passed for item in self.items))

    def report_text(self) -> str:
        lines: list[str] = []
        for item in self.items:
            state = "PASS" if item.passed else "FAIL"
            lines.append(
                f"[{state}] {item.name}: {item.measured} ({'required'}: {item.requirement})"
            )
            if item.detail:
                lines.append(f"       {item.detail}")
        lines.append("")
        lines.append(
            "Note: width/spacing checks are geometric approximations and do not replace formal fab or metrology DRC."
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class PathSettings:
    hatch_spacing_mm: float = 0.025
    laser_spot_mm: float = 0.04
    feed_mm_min: float = 5.0
    acceleration_mm_s2: float = 5.0
    laser_power: int = 500
    max_laser_power: int = 1000
    overscan_mm: float = 0.2
    row_settle_ms: int = 20
    x_steps_per_mm: float = 200.0
    y_steps_per_mm: float = 200.0
    return_to_origin: bool = False
    x_backlash_um: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (
            ("Measured writing width", self.laser_spot_mm),
            ("Maximum hatch spacing", self.hatch_spacing_mm),
            ("X steps/mm", self.x_steps_per_mm),
            ("Y steps/mm", self.y_steps_per_mm),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and greater than zero.")
        if (
            not math.isfinite(self.x_backlash_um)
            or not -100 <= self.x_backlash_um <= 100
        ):
            raise ValueError(
                "X bidirectional compensation must be finite and within [-100, 100] um."
            )
        for name, value in (
            ("Feed/mm/min", self.feed_mm_min),
            ("Acceleration/mm/s²", self.acceleration_mm_s2),
        ):
            if not math.isfinite(value) or not 0 < value <= 10:
                raise ValueError(f"{name} must be finite and in (0, 10].")

    @property
    def theoretical_accel_distance_mm(self) -> float:
        speed_mm_s = self.feed_mm_min / 60.0
        if self.acceleration_mm_s2 <= 0:
            return math.inf
        return speed_mm_s * speed_mm_s / (2.0 * self.acceleration_mm_s2)


@dataclass(frozen=True)
class ScanRow:
    y_mm: float
    left_to_right: bool
    exposure_intervals: tuple[tuple[float, float], ...]


@dataclass
class PathPlan:
    rows: list[ScanRow]
    left_mm: float
    right_mm: float
    bottom_mm: float
    top_mm: float
    design_width_mm: float
    design_height_mm: float
    design_x_offset_mm: float
    source_geometry: Any
    exposure_width_mm: float = 0.04
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MotionSegment:
    line_index: int
    start_x_mm: float
    start_y_mm: float
    end_x_mm: float
    end_y_mm: float
    feed_mm_min: float
    laser_on: bool
    power: float
    rapid: bool

    @property
    def length_mm(self) -> float:
        return math.hypot(
            self.end_x_mm - self.start_x_mm, self.end_y_mm - self.start_y_mm
        )


@dataclass
class GCodeAnalysis:
    segments: list[MotionSegment]
    errors: list[str]
    warnings: list[str]
    min_x_mm: float
    min_y_mm: float
    max_x_mm: float
    max_y_mm: float
    burn_length_mm: float
    travel_length_mm: float
    estimated_seconds: float
    line_count: int

    @property
    def valid(self) -> bool:
        return not self.errors

    def summary_text(self) -> str:
        estimate = format_duration(self.estimated_seconds)
        return f"Lines {self.line_count} | range X {self.min_x_mm:.4f}…{self.max_x_mm:.4f} mm, Y {self.min_y_mm:.4f}…{self.max_y_mm:.4f} mm | exposure {self.burn_length_mm:.2f} mm | travel {self.travel_length_mm:.2f} mm | estimated {estimate}"


@dataclass
class GCodeProgram:
    lines: list[str]
    analysis: GCodeAnalysis
    path_plan: PathPlan | None = None
    output_path: Path | None = None
    simulation_path: Path | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.lines).rstrip() + "\n"

    @property
    def digest(self) -> str:
        return text_digest(self.text)


def text_digest(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def format_duration(seconds: float) -> str:
    if not math.isfinite(seconds):
        return "Unknown"
    seconds_i = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds_i, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d} h {minutes:02d} min {secs:02d} s"
    return f"{minutes:d} min {secs:02d} s"


def load_gds(path: str | Path) -> GdsDocument:
    source = Path(path).expanduser().resolve()
    if source.suffix.lower() not in {".gds", ".gdsii"}:
        raise ValueError("Select a .gds or .gdsii file.")
    library = gdstk.read_gds(str(source))
    cells = {cell.name: cell for cell in library.top_level()}
    if not cells:
        cells = {cell.name: cell for cell in library.cells}
    if not cells:
        raise ValueError("The GDS file contains no usable Cell.")
    return GdsDocument(
        path=source, library=library, scale_to_mm=library.unit * 1000.0, cells=cells
    )


def list_layers(document: GdsDocument, cell_name: str) -> list[LayerInfo]:
    cell = document.cells[cell_name]
    grouped: dict[tuple[int, int], list[gdstk.Polygon]] = {}
    for polygon in cell.get_polygons(
        apply_repetitions=True, include_paths=True, depth=None
    ):
        grouped.setdefault((polygon.layer, polygon.datatype), []).append(polygon)
    result: list[LayerInfo] = []
    for (layer, datatype), polygons in sorted(grouped.items()):
        min_x = math.inf
        min_y = math.inf
        max_x = -math.inf
        max_y = -math.inf
        for polygon in polygons:
            bounds = polygon.bounding_box()
            if bounds is None:
                continue
            (x0, y0), (x1, y1) = bounds
            min_x = min(min_x, float(x0) * document.scale_to_mm)
            min_y = min(min_y, float(y0) * document.scale_to_mm)
            max_x = max(max_x, float(x1) * document.scale_to_mm)
            max_y = max(max_y, float(y1) * document.scale_to_mm)
        if not math.isfinite(min_x):
            min_x = min_y = max_x = max_y = 0.0
        result.append(
            LayerInfo(
                layer=layer,
                datatype=datatype,
                polygon_count=len(polygons),
                min_x_mm=min_x,
                min_y_mm=min_y,
                max_x_mm=max_x,
                max_y_mm=max_y,
            )
        )
    return result


def polygons_for_layer(
    document: GdsDocument,
    cell_name: str,
    layer: int,
    datatype: int,
    *,
    shift_to_origin: bool,
) -> list[Polygon]:
    cell = document.cells[cell_name]
    raw_polygons = cell.get_polygons(
        apply_repetitions=True,
        include_paths=True,
        depth=None,
        layer=layer,
        datatype=datatype,
    )
    converted: list[Polygon] = []
    for raw in raw_polygons:
        points = [
            (float(x) * document.scale_to_mm, float(y) * document.scale_to_mm)
            for x, y in raw.points
        ]
        if len(points) < 3:
            continue
        shape: Any = Polygon(points)
        if not shape.is_valid:
            shape = shape.buffer(0)
        if shape.is_empty:
            continue
        if isinstance(shape, Polygon):
            converted.append(shape)
        elif isinstance(shape, MultiPolygon):
            converted.extend((part for part in shape.geoms if not part.is_empty))
    if not converted or not shift_to_origin:
        return converted
    min_x = min((poly.bounds[0] for poly in converted))
    min_y = min((poly.bounds[1] for poly in converted))
    shifted: list[Polygon] = []
    for poly in converted:
        exterior = [(x - min_x, y - min_y) for x, y in poly.exterior.coords]
        holes = [
            [(x - min_x, y - min_y) for x, y in ring.coords] for ring in poly.interiors
        ]
        shifted.append(Polygon(exterior, holes))
    return shifted


def extract_polygon_components(geometry: Any) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        result: list[Polygon] = []
        for part in geometry.geoms:
            result.extend(extract_polygon_components(part))
        return result
    return []


def estimate_min_feature(polygons: Sequence[Polygon]) -> float:
    candidates: list[float] = []
    for polygon in polygons:
        min_x, min_y, max_x, max_y = polygon.bounds
        box_width = min(max_x - min_x, max_y - min_y)
        if box_width > EPSILON_MM:
            candidates.append(box_width)
        clearance = polygon.minimum_clearance
        if math.isfinite(clearance) and clearance > EPSILON_MM:
            candidates.append(clearance)
    return min(candidates) if candidates else 0.0


def estimate_min_spacing(
    polygons: Sequence[Polygon], search_distance_mm: float
) -> float | None:
    merged = unary_union([polygon for polygon in polygons if not polygon.is_empty])
    components = extract_polygon_components(merged)
    if len(components) < 2:
        return None
    tree = STRtree(components)
    by_identity = {id(shape): index for index, shape in enumerate(components)}
    minimum: float | None = None
    for index, component in enumerate(components):
        for candidate in tree.query(component.buffer(search_distance_mm + EPSILON_MM)):
            if hasattr(candidate, "item"):
                candidate = candidate.item()
            if isinstance(candidate, int):
                other_index = candidate
            else:
                other_index = by_identity.get(id(candidate), -1)
            if other_index <= index:
                continue
            distance = component.distance(components[other_index])
            if distance <= EPSILON_MM:
                continue
            if minimum is None or distance < minimum:
                minimum = distance
    return minimum


def run_drc(polygons: Sequence[Polygon], rules: DesignRules) -> DrcResult:
    if not polygons:
        return DrcResult(
            items=[
                CheckItem(
                    "Geometry exists",
                    "No geometry",
                    "At least one valid polygon",
                    False,
                )
            ],
            min_feature_mm=0.0,
            min_spacing_mm=None,
        )
    geometry = unary_union(polygons)
    min_x, min_y, max_x, max_y = geometry.bounds
    width = max_x - min_x
    height = max_y - min_y
    min_feature = estimate_min_feature(polygons)
    min_spacing = estimate_min_spacing(polygons, rules.min_spacing_mm)
    x_resolution = math.inf if rules.x_steps_per_mm <= 0 else 1.0 / rules.x_steps_per_mm
    y_resolution = math.inf if rules.y_steps_per_mm <= 0 else 1.0 / rules.y_steps_per_mm
    step_resolution = max(x_resolution, y_resolution)
    spacing_measurement = (
        f">= {rules.min_spacing_mm * 1000.0:.1f} µm or one continuous shape"
        if min_spacing is None
        else f"{min_spacing * 1000.0:.1f} µm"
    )
    items = [
        CheckItem(
            "Exposure bounds",
            f"{width:.4f} × {height:.4f} mm",
            f"<= {rules.max_width_mm:g} × {rules.max_height_mm:g} mm",
            width <= rules.max_width_mm + EPSILON_MM
            and height <= rules.max_height_mm + EPSILON_MM,
        ),
        CheckItem(
            "Minimum feature",
            f"{min_feature * 1000.0:.1f} µm",
            f">= {rules.min_feature_mm * 1000.0:.1f} µm",
            min_feature + EPSILON_MM >= rules.min_feature_mm,
            "Estimated from polygon bounding boxes and minimum_clearance.",
        ),
        CheckItem(
            "Geometry spacing",
            spacing_measurement,
            f">= {rules.min_spacing_mm * 1000.0:.1f} µm",
            min_spacing is None or min_spacing + EPSILON_MM >= rules.min_spacing_mm,
        ),
        CheckItem(
            "Motor command resolution",
            f"X {x_resolution * 1000.0:.2f} µm，Y {y_resolution * 1000.0:.2f} µm",
            f"<= {rules.target_precision_mm * 1000.0:.1f} µm",
            step_resolution <= rules.target_precision_mm + EPSILON_MM,
            "Calculated as 1/steps_per_mm; backlash, stiffness and missed steps are excluded.",
        ),
        CheckItem(
            "Hatch spacing",
            f"{rules.hatch_spacing_mm * 1000.0:.1f} µm",
            f"<= {rules.target_precision_mm * 1000.0:.1f} µm",
            0 < rules.hatch_spacing_mm <= rules.target_precision_mm + EPSILON_MM,
        ),
    ]
    return DrcResult(
        items=items, min_feature_mm=min_feature, min_spacing_mm=min_spacing
    )


def _extract_intervals(geometry: Any) -> list[tuple[float, float]]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        xs = [float(point[0]) for point in geometry.coords]
        if not xs:
            return []
        left, right = (min(xs), max(xs))
        return [(left, right)] if right - left > EPSILON_MM else []
    if isinstance(geometry, (MultiLineString, GeometryCollection)):
        result: list[tuple[float, float]] = []
        for part in geometry.geoms:
            result.extend(_extract_intervals(part))
        return result
    return []


def _quantize(value: float, steps_per_mm: float) -> float:
    if steps_per_mm <= 0:
        raise ValueError("steps_per_mm must be greater than 0.")
    return round(value * steps_per_mm) / steps_per_mm


def _merge_intervals(
    intervals: Iterable[tuple[float, float]], tolerance: float
) -> list[tuple[float, float]]:
    ordered = sorted(
        ((min(a, b), max(a, b)) for a, b in intervals if abs(b - a) > EPSILON_MM)
    )
    if not ordered:
        return []
    merged = [ordered[0]]
    for left, right in ordered[1:]:
        old_left, old_right = merged[-1]
        if left <= old_right + tolerance:
            merged[-1] = (old_left, max(old_right, right))
        else:
            merged.append((left, right))
    return merged


def _coverage_row_ticks(
    low: float, high: float, origin: float, settings: PathSettings
) -> list[int]:
    """Y center rows, inset by half a measured width, on the controller grid.

    The maximum gap is bounded in integer steps, so rounding cannot create
    a larger gap. The inset positions can differ by at most half a Y step.
    """
    width = settings.laser_spot_mm
    scale = settings.y_steps_per_mm
    if high - low <= width + EPSILON_MM:
        return [round(((low + high) / 2 - origin) * scale)]
    first = round((low + width / 2 - origin) * scale)
    last = round((high - width / 2 - origin) * scale)
    max_ticks = math.floor(min(settings.hatch_spacing_mm, width) * scale + 1e-09)
    if max_ticks < 1:
        raise ValueError(
            "Y step exceeds writing width or maximum hatch spacing; check actual steps/mm."
        )
    span = last - first
    if span <= 0:
        return [first]
    intervals = math.ceil(span / max_ticks)
    if intervals > 200000:
        raise ValueError("Too many scan rows; check writing-width and hatch units.")
    return [first + round(i * span / intervals) for i in range(intervals + 1)]


def generate_serpentine_plan(geometry: Any, settings: PathSettings) -> PathPlan:
    if geometry.is_empty:
        raise ValueError(
            "The selected Layer has no geometry from which to generate a path."
        )
    if settings.hatch_spacing_mm <= 0 or settings.feed_mm_min <= 0:
        raise ValueError("Hatch spacing and feed must be greater than 0.")
    if settings.overscan_mm < settings.theoretical_accel_distance_mm:
        raise ValueError(
            f"Overscan {settings.overscan_mm:.4f} mm is less than the theoretical acceleration distance {settings.theoretical_accel_distance_mm:.4f} mm."
        )
    if not 0 <= settings.laser_power <= settings.max_laser_power:
        raise ValueError("Laser power is outside the allowed range.")
    min_x, min_y, max_x, max_y = geometry.bounds
    design_width = max_x - min_x
    design_height = max_y - min_y
    left = 0.0
    right = _quantize(
        design_width + 2.0 * settings.overscan_mm, settings.x_steps_per_mm
    )
    x_offset = _quantize(settings.overscan_mm - min_x, settings.x_steps_per_mm)
    line_left = min_x - max(1.0, settings.overscan_mm + 0.1)
    line_right = max_x + max(1.0, settings.overscan_mm + 0.1)
    rows: list[ScanRow] = []
    warnings: list[str] = []
    x_step = 1.0 / settings.x_steps_per_mm
    reverse_shift = -_quantize(settings.x_backlash_um / 1000.0, settings.x_steps_per_mm)
    if (
        settings.x_backlash_um
        and abs(reverse_shift) + settings.theoretical_accel_distance_mm
        > settings.overscan_mm + EPSILON_MM
    ):
        raise ValueError(
            "X compensation plus acceleration distance exceeds overscan; increase overscan or reduce compensation."
        )
    if settings.hatch_spacing_mm > settings.laser_spot_mm + EPSILON_MM:
        warnings.append(
            "Maximum hatch exceeds measured width; actual spacing is reduced to at most that width. Recheck exposure dose."
        )
    intervals_by_tick: dict[int, list[tuple[float, float]]] = {}
    components = extract_polygon_components(geometry)
    for component in components:
        low, high = (component.bounds[1], component.bounds[3])
        if high - low < settings.laser_spot_mm - EPSILON_MM:
            warnings.append(
                "A component is narrower than the writing width in Y: one centered row may overrun the target."
            )
        if (
            component.interiors
            or abs(component.area - component.envelope.area) > EPSILON_MM**2
        ):
            warnings.append(
                "Nonrectangular/holed components use their top/bottom bounds; sloped edges, holes and thin branches may have gaps or overrun. Inspect coverage against outlines."
            )
        for tick in _coverage_row_ticks(low, high, min_y, settings):
            source_y = min_y + tick / settings.y_steps_per_mm
            if source_y < low - EPSILON_MM or source_y > high + EPSILON_MM:
                raise ValueError(
                    "Y quantization puts a row outside a thin component; check steps/mm and geometry."
                )
            source_y = min(high, max(low, source_y))
            scan_line = LineString([(line_left, source_y), (line_right, source_y)])
            raw = _extract_intervals(component.intersection(scan_line))
            if raw:
                intervals_by_tick.setdefault(tick, []).extend(raw)
        if len(intervals_by_tick) > 200000:
            raise ValueError("Too many scan rows; check parameters.")
    for tick, raw_intervals in sorted(intervals_by_tick.items()):
        output_y = tick / settings.y_steps_per_mm
        shifted = [
            (
                _quantize(start + x_offset, settings.x_steps_per_mm),
                _quantize(end + x_offset, settings.x_steps_per_mm),
            )
            for start, end in raw_intervals
        ]
        intervals = _merge_intervals(shifted, tolerance=x_step * 0.5)
        intervals = [
            (max(left, start), min(right, end))
            for start, end in intervals
            if min(right, end) - max(left, start) >= x_step * 0.5
        ]
        if not intervals:
            continue
        left_to_right = len(rows) % 2 == 0
        if not left_to_right and reverse_shift:
            intervals = [
                (start + reverse_shift, end + reverse_shift) for start, end in intervals
            ]
            if any(
                (
                    start < left - EPSILON_MM or end > right + EPSILON_MM
                    for start, end in intervals
                )
            ):
                raise ValueError(
                    "Compensated exposure exceeds scan bounds; increase overscan."
                )
        rows.append(
            ScanRow(
                y_mm=output_y,
                left_to_right=left_to_right,
                exposure_intervals=tuple(intervals),
            )
        )
    if not rows:
        raise ValueError(
            "No valid scan row was generated; check the Layer and hatch spacing."
        )
    return PathPlan(
        rows=rows,
        left_mm=left,
        right_mm=right,
        bottom_mm=rows[0].y_mm,
        top_mm=rows[-1].y_mm,
        design_width_mm=design_width,
        design_height_mm=design_height,
        design_x_offset_mm=x_offset,
        source_geometry=geometry,
        exposure_width_mm=settings.laser_spot_mm,
        warnings=list(dict.fromkeys(warnings)),
    )


def _fmt_coord(value: float) -> str:
    if abs(value) < 5e-07:
        value = 0.0
    return f"{value:.4f}"


def _fmt_feed(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def build_gcode(
    plan: PathPlan,
    settings: PathSettings,
    *,
    source_path: Path,
    cell_name: str,
    layer_info: LayerInfo,
    machine_width_mm: float,
    machine_height_mm: float,
) -> GCodeProgram:
    if (
        plan.right_mm > machine_width_mm + EPSILON_MM
        or plan.top_mm > machine_height_mm + EPSILON_MM
    ):
        raise ValueError(
            f"Path bounds {plan.right_mm:.4f} × {plan.top_mm:.4f} mm exceed machine travel {machine_width_mm:.4f} × {machine_height_mm:.4f} mm."
        )
    lines = [
        "; Generated by FluidNC GDS Laser Writer",
        f"; App version: {APP_VERSION}",
        f"; Source: {source_path}",
        f"; Cell: {cell_name}",
        f"; Layer/Datatype: {layer_info.layer}/{layer_info.datatype}",
        f"; Design size: {plan.design_width_mm:.4f} x {plan.design_height_mm:.4f} mm",
        f"; Hatch spacing: {settings.hatch_spacing_mm:.4f} mm",
        f"; Measured writing width: {settings.laser_spot_mm:.4f} mm",
        "; Hatch policy: maximum spacing; center rows inset half-width and evenly redistributed per component",
        "; Coverage is geometric, not an optical-dose or developed-linewidth prediction",
        f"; Feed: {settings.feed_mm_min:.3f} mm/min",
        f"; Laser power: {settings.laser_power}/{settings.max_laser_power}",
        f"; Overscan: {settings.overscan_mm:.4f} mm",
        f"; Theoretical acceleration distance: {settings.theoretical_accel_distance_mm:.4f} mm",
        "; Motion policy: uniform commanded feed for exposure and laser-off travel; G1 only",
        "; IMPORTANT: Set work zero at the lower-left safe start before running.",
        "G21",
        "G90",
        "G94",
        "M5",
        f"G1 X0.0000 Y0.0000 F{_fmt_feed(settings.feed_mm_min)}",
        "M3 S0",
    ]

    def dark_move(end, y):
        lines.append(
            f"G1 X{_fmt_coord(end)} Y{_fmt_coord(y)} F{_fmt_feed(settings.feed_mm_min)} S0"
        )

    if settings.x_backlash_um:
        shift_um = (
            -_quantize(settings.x_backlash_um / 1000.0, settings.x_steps_per_mm)
            * 1000.0
        )
        lines.insert(
            0,
            f"; X bidirectional compensation: {settings.x_backlash_um:+.4f} um; reverse exposure X shift: {shift_um:+.4f} um (step-quantized); forward unchanged",
        )
    for row_number, row in enumerate(plan.rows, 1):
        start_x = plan.left_mm if row.left_to_right else plan.right_mm
        end_x = plan.right_mm if row.left_to_right else plan.left_mm
        lines.append(
            f"G1 X{_fmt_coord(start_x)} Y{_fmt_coord(row.y_mm)} F{_fmt_feed(settings.feed_mm_min)} S0"
        )
        if settings.row_settle_ms > 0:
            lines.append(f"G4 P{settings.row_settle_ms / 1000.0:.3f}")
        intervals = (
            row.exposure_intervals
            if row.left_to_right
            else tuple(reversed(row.exposure_intervals))
        )
        current_x = start_x
        for left, right in intervals:
            entry_x = left if row.left_to_right else right
            exit_x = right if row.left_to_right else left
            if abs(entry_x - current_x) > EPSILON_MM:
                dark_move(entry_x, row.y_mm)
            if abs(exit_x - entry_x) > EPSILON_MM:
                lines.append(
                    f"G1 X{_fmt_coord(exit_x)} Y{_fmt_coord(row.y_mm)} F{_fmt_feed(settings.feed_mm_min)} S{settings.laser_power}"
                )
            current_x = exit_x
        if abs(end_x - current_x) > EPSILON_MM:
            dark_move(end_x, row.y_mm)
        lines.append("S0")
        if settings.row_settle_ms > 0:
            lines.append(f"G4 P{settings.row_settle_ms / 1000.0:.3f}")
        lines.append(
            f"; ROW_END {row_number} X{_fmt_coord(end_x)} Y{_fmt_coord(row.y_mm)}"
        )
    lines.append("M5")
    if settings.return_to_origin:
        lines.append(f"G1 X0.0000 Y0.0000 F{_fmt_feed(settings.feed_mm_min)}")
        lines.append("M5")
    lines.append("M2")
    text = "\n".join(lines) + "\n"
    analysis = analyze_gcode(
        text,
        machine_width_mm=machine_width_mm,
        machine_height_mm=machine_height_mm,
        max_feed_mm_min=max(settings.feed_mm_min, 1.0),
        max_laser_power=settings.max_laser_power,
    )
    if analysis.errors:
        raise ValueError(
            "Generated G-code failed internal validation:"
            + "\n"
            + "\n".join(analysis.errors)
        )
    analysis.warnings.extend(plan.warnings)
    return GCodeProgram(lines=lines, analysis=analysis, path_plan=plan)


_WORD_RE = re.compile("([A-Z])\\s*([-+]?(?:\\d+(?:\\.\\d*)?|\\.\\d+))", re.IGNORECASE)


def _strip_comments(line: str) -> str:
    result = line.split(";", 1)[0]
    result = re.sub("\\([^)]*\\)", "", result)
    return result.strip().upper()


def analyze_gcode(
    text: str,
    *,
    machine_width_mm: float,
    machine_height_mm: float,
    max_feed_mm_min: float,
    max_laser_power: int,
) -> GCodeAnalysis:
    max_feed_mm_min = min(max_feed_mm_min, 10.0)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    errors: list[str] = []
    warnings: list[str] = []
    segments: list[MotionSegment] = []
    x = y = 0.0
    units_scale = 1.0
    absolute = True
    motion_mode: int | None = None
    feed = 0.0
    power = 0.0
    spindle_enabled = False
    saw_metric = False
    saw_absolute = False
    saw_m5 = False
    min_x = max_x = x
    min_y = max_y = y
    dwell_seconds = 0.0
    rapid_count = 0
    for line_index, raw_line in enumerate(lines):
        line_number = line_index + 1
        line = _strip_comments(raw_line)
        if not line:
            continue
        if "$" in line or "%" in line:
            errors.append(
                f"Line {line_number} contains a control character that cannot be sent with a file."
            )
            continue
        if re.search("\\b[ZABC]", line):
            errors.append(f"Line {line_number} contains a non-XY axis command.")
        words = [
            (letter.upper(), float(value)) for letter, value in _WORD_RE.findall(line)
        ]
        if not words:
            errors.append(f"Line {line_number} is not recognized: {raw_line.strip()}")
            continue
        target_x = x
        target_y = y
        line_motion = motion_mode
        has_axis = False
        dwell_p: float | None = None
        line_g_codes: list[int] = []
        for letter, value in words:
            if letter == "G":
                code = int(round(value))
                line_g_codes.append(code)
                if code == 20:
                    units_scale = 25.4
                    warnings.append(f"Line {line_number} uses inch units G20.")
                elif code == 21:
                    units_scale = 1.0
                    saw_metric = True
                elif code == 90:
                    absolute = True
                    saw_absolute = True
                elif code == 91:
                    absolute = False
                elif code in (0, 1):
                    line_motion = code
                    motion_mode = code
                    if code == 0:
                        rapid_count += 1
                elif code in (4, 94):
                    pass
                else:
                    errors.append(
                        f"Line {line_number} contains unvalidated G{value:g}. Only G0/G1/G4/G20/G21/G90/G91/G94 are allowed."
                    )
            elif letter == "M":
                code = int(round(value))
                if code in (3, 4):
                    spindle_enabled = True
                elif code == 5:
                    spindle_enabled = False
                    power = 0.0
                    saw_m5 = True
                elif code in (2, 30):
                    pass
                else:
                    errors.append(
                        f"Line {line_number} contains unvalidated M{value:g}."
                    )
            elif letter == "F":
                feed = value * units_scale
                if feed <= 0:
                    errors.append(f"Feed on line {line_number} must be greater than 0.")
                elif feed > max_feed_mm_min + EPSILON_MM:
                    errors.append(
                        f"Feed F{feed:g} on line {line_number} exceeds the review limit {max_feed_mm_min:g} mm/min."
                    )
            elif letter == "S":
                power = value
                if power < 0 or power > max_laser_power:
                    errors.append(
                        f"Laser power S{power:g} on line {line_number} is outside 0…{max_laser_power}."
                    )
            elif letter == "X":
                converted = value * units_scale
                target_x = converted if absolute else x + converted
                has_axis = True
            elif letter == "Y":
                converted = value * units_scale
                target_y = converted if absolute else y + converted
                has_axis = True
            elif letter == "P":
                dwell_p = value
            elif letter in {"N"}:
                pass
            else:
                errors.append(
                    f"Line {line_number} contains unvalidated parameter {letter}."
                )
        if 4 in line_g_codes and dwell_p is not None:
            dwell_seconds += max(0.0, dwell_p)
        if has_axis:
            if line_motion not in (0, 1):
                errors.append(
                    f"Line {line_number} has coordinates but no valid G0/G1 mode."
                )
            else:
                if line_motion == 1 and feed <= 0:
                    errors.append(f"No valid F is set before G1 on line {line_number}.")
                laser_on = line_motion == 1 and spindle_enabled and (power > 0)
                segment_feed = feed if line_motion == 1 else max_feed_mm_min
                segment = MotionSegment(
                    line_index=line_index,
                    start_x_mm=x,
                    start_y_mm=y,
                    end_x_mm=target_x,
                    end_y_mm=target_y,
                    feed_mm_min=segment_feed,
                    laser_on=laser_on,
                    power=power if laser_on else 0.0,
                    rapid=line_motion == 0,
                )
                if segment.length_mm > EPSILON_MM:
                    segments.append(segment)
                x, y = (target_x, target_y)
                min_x, max_x = (min(min_x, x), max(max_x, x))
                min_y, max_y = (min(min_y, y), max(max_y, y))
                if x < -EPSILON_MM or y < -EPSILON_MM:
                    errors.append(
                        f"Line {line_number} moves to negative coordinates X{x:.4f} Y{y:.4f}."
                    )
                if (
                    x > machine_width_mm + EPSILON_MM
                    or y > machine_height_mm + EPSILON_MM
                ):
                    errors.append(
                        f"Line {line_number} exceeds machine travel: X{x:.4f} Y{y:.4f} mm."
                    )
    if not saw_metric:
        errors.append("Missing G21 metric-units command.")
    if not saw_absolute:
        errors.append("Missing G90 absolute-coordinate command.")
    if not saw_m5:
        errors.append("The program has no M5 laser-off command.")
    if spindle_enabled or power > 0:
        errors.append(
            "The laser may still be on at program end; the program must end with M5."
        )
    if rapid_count:
        warnings.append(
            f"Detected {rapid_count} G0 move(s); safe serpentine mode recommends speed-limited G1 throughout."
        )
    burn_length = sum((segment.length_mm for segment in segments if segment.laser_on))
    travel_length = sum(
        (segment.length_mm for segment in segments if not segment.laser_on)
    )
    estimated_seconds = dwell_seconds
    for segment in segments:
        if segment.feed_mm_min > 0:
            estimated_seconds += segment.length_mm / segment.feed_mm_min * 60.0
    return GCodeAnalysis(
        segments=segments,
        errors=list(dict.fromkeys(errors)),
        warnings=list(dict.fromkeys(warnings)),
        min_x_mm=min_x,
        min_y_mm=min_y,
        max_x_mm=max_x,
        max_y_mm=max_y,
        burn_length_mm=burn_length,
        travel_length_mm=travel_length,
        estimated_seconds=estimated_seconds,
        line_count=len(lines),
    )


def exposure_footprints(
    segments: Sequence[MotionSegment], width_mm: float
) -> list[Polygon]:
    """Circular-spot swept areas in machine mm, not screen/print points."""
    if not math.isfinite(width_mm) or width_mm <= 0:
        raise ValueError("Exposure width must be finite and greater than zero.")
    return [
        LineString([(s.start_x_mm, s.start_y_mm), (s.end_x_mm, s.end_y_mm)]).buffer(
            width_mm / 2.0, quad_segs=8
        )
        for s in segments
        if s.laser_on and s.length_mm > EPSILON_MM
    ]


def add_exposure_coverage(axis, segments: Sequence[MotionSegment], width_mm: float):
    from matplotlib.collections import PolyCollection

    footprints = exposure_footprints(segments, width_mm)
    if not footprints:
        return None
    collection = PolyCollection(
        [list(p.exterior.coords) for p in footprints],
        facecolors=(0.88, 0.05, 0.05, 0.38),
        edgecolors="none",
        label="Exposure coverage (darker overlap, not dose)",
    )
    axis.add_collection(collection)
    return collection


def save_simulation_png(
    program: GCodeProgram, path: str | Path, laser_spot_mm: float
) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    output = Path(path).resolve()
    travel = []
    burn = []
    powers = []
    for segment in program.analysis.segments:
        points = [
            (segment.start_x_mm, segment.start_y_mm),
            (segment.end_x_mm, segment.end_y_mm),
        ]
        if segment.laser_on:
            burn.append(points)
            powers.append(segment.power)
        else:
            travel.append(points)
    fig, axis = plt.subplots(figsize=(8, 8), dpi=180)
    if travel:
        axis.add_collection(
            LineCollection(travel, colors="#6a93b8", linewidths=0.25, alpha=0.35)
        )
    width = program.path_plan.exposure_width_mm if program.path_plan else laser_spot_mm
    add_exposure_coverage(axis, program.analysis.segments, width)
    if program.path_plan:
        from shapely import affinity

        plan = program.path_plan
        outline = affinity.translate(
            plan.source_geometry,
            xoff=plan.design_x_offset_mm,
            yoff=-plan.source_geometry.bounds[1],
        )
        for polygon in extract_polygon_components(outline):
            axis.plot(*polygon.exterior.xy, color="#222222", linewidth=0.5)
            for ring in polygon.interiors:
                axis.plot(*ring.xy, color="#222222", linewidth=0.5)
    axis.autoscale()
    axis.set_aspect("equal", adjustable="box")
    axis.set_xlabel("X / mm")
    axis.set_ylabel("Y / mm")
    axis.set_title(f"Geometric exposure coverage · width {width * 1000:g} um")
    axis.grid(True, linewidth=0.3, alpha=0.4)
    fig.tight_layout()
    fig.savefig(output)
    plt.close(fig)
    return output
