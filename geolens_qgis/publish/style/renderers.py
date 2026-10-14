"""Turn a QGIS feature renderer into GeoLens paint, style_config and a layer filter."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from qgis.core import QgsExpression, QgsFeatureRequest, QgsRenderContext

from .loss import LossLog
from .ramps import HEATMAP_SAMPLES, heatmap_color_expression, nearest_ramp
from .symbols import COLOR_PROPERTY, SIZE_PROPERTY, SymbolStyle, convert_symbol
from .units import to_pixels

FALLBACK_COLOR = "#cccccc"
# A ramp this far (mean channel value, 0-255) from every named ramp is a custom one.
RAMP_MATCH_TOLERANCE = 6
# QMetaType ids of the integer field types.
_INTEGER_TYPES = {2, 3, 4, 5, 32, 33, 35, 36}
_METHODS = {
    "EqualInterval": "equal_interval",
    "Quantile": "quantile",
    "Jenks": "jenks",
    "StdDev": "std_dev",
}


@dataclass
class RenderedStyle:
    paint: dict[str, Any] = field(default_factory=dict)
    layout: dict[str, Any] = field(default_factory=dict)
    style_config: dict[str, Any] | None = None
    filter: list | None = None


def _is_null(value) -> bool:
    return value is None or bool(getattr(value, "isNull", lambda: False)())


def _field_name(layer, expression: str) -> str | None:
    """Return the field a class attribute names, or None for a real expression."""
    name = expression.strip()
    if len(name) > 1 and name[0] == name[-1] == '"':
        name = name[1:-1].replace('""', '"')
    return name if name in layer.fields().names() else None


def _base(style: SymbolStyle) -> RenderedStyle:
    """Paint, layout and builder state of a symbol, with its colour in place."""
    paint = dict(style.paint)
    if style.color is not None:
        paint[COLOR_PROPERTY[style.kind]] = style.color
    config = {"builder": dict(style.builder)} if style.builder else None
    return RenderedStyle(paint, dict(style.layout), config)


def _first_symbol(renderer):
    symbols = renderer.symbols(QgsRenderContext())
    return symbols[0] if symbols else None


def _unsupported(renderer, losses: LossLog, detail: str) -> RenderedStyle:
    """Fall back to a single symbol in the renderer's first symbol."""
    losses.add("unsupported_renderer", "renderer", detail)
    symbol = _first_symbol(renderer) if renderer is not None else None
    if symbol is None:
        return RenderedStyle()
    return _base(convert_symbol(symbol, losses, "renderer"))


def _single(renderer, losses: LossLog) -> RenderedStyle:
    return _base(convert_symbol(renderer.symbol(), losses, "renderer.symbol"))


@dataclass
class _Class:
    style: SymbolStyle
    label: str
    enabled: bool


def _class_styles(items, losses: LossLog, where: str) -> list[_Class]:
    """Convert every class symbol; classes may differ from the first only in colour."""
    classes = []
    for index, (symbol, label, enabled) in enumerate(items):
        path = f"{where}[{index}]"
        style = convert_symbol(symbol, losses, path)
        if classes and style.fixed() != classes[0].style.fixed():
            losses.add("symbol_layer_dropped", path, "differs in more than colour")
        classes.append(_Class(style, label, enabled))
    return classes


def _step(column: str, values: list, breaks: list[float], default) -> list:
    step: list = ["step", ["get", column], values[0]]
    for edge, value in zip(breaks, values[1:]):
        step += [edge, value]
    return ["case", ["==", ["get", column], None], default, step]


def _categorized(layer, renderer, losses: LossLog) -> RenderedStyle:
    categories = renderer.categories()
    column = _class_column(layer, renderer, categories, losses)
    if column is None:
        return _first_class(categories, renderer, losses)
    styles = _class_styles(
        [(c.symbol(), c.label(), c.renderState()) for c in categories],
        losses,
        "renderer.categories",
    )
    listed, seen, other, null = [], set(), None, None
    for category, entry in zip(categories, styles):
        value = category.value()
        if _is_null(value):
            null = null or entry
        elif value == "":
            other = other or entry
        else:
            for item in value if isinstance(value, list) else [value]:
                if item not in seen:
                    seen.add(item)
                    listed.append((item, entry))
    fallback = other.style.color if other else FALLBACK_COLOR
    if null is not None and null.style.color != fallback:
        losses.add(
            "null_class_changed",
            "renderer.categories",
            f"nulls drawn {null.style.color} in QGIS, {fallback} in GeoLens",
        )
    result = _base(styles[0].style)
    pairs = [
        value_color
        for value, entry in listed
        for value_color in (value, entry.style.color)
    ]
    color_expression = ["case", ["==", ["get", column], None], fallback]
    if pairs:
        color_expression.append(["match", ["get", column], *pairs, fallback])
    else:
        color_expression = fallback
    result.paint[COLOR_PROPERTY[styles[0].style.kind]] = color_expression
    result.style_config = {
        **(result.style_config or {}),
        "mode": "categorical",
        "column": column,
        "categories": [
            {"value": value, "label": entry.label, "color": entry.style.color}
            for value, entry in listed
        ],
    }
    result.filter = _category_filter(column, listed, other, null)
    return result


def _class_column(layer, renderer, classes, losses: LossLog) -> str | None:
    """Return the field a class renderer reads, or None when it can't be drawn as classes."""
    if not classes:
        return None
    column = _field_name(layer, renderer.classAttribute())
    if column is None:
        losses.add(
            "expression_not_representable",
            "renderer.classAttribute",
            renderer.classAttribute(),
        )
    return column


def _first_class(classes, renderer, losses: LossLog) -> RenderedStyle:
    """Draw a class renderer that can't be described as classes with its first symbol."""
    if not classes:
        return _unsupported(renderer, losses, "no classes")
    return _base(convert_symbol(classes[0].symbol(), losses, "renderer.symbol"))


def _category_filter(column, listed, other, null) -> list | None:
    """Hide the classes the QGIS user switched off."""
    value = ["get", column]
    disabled = [v for v, entry in listed if not entry.enabled]
    if other is not None and not other.enabled:
        shown: list = [["in", value, ["literal", [v for v, e in listed if e.enabled]]]]
        if null is None or null.enabled:
            shown.append(["==", value, None])
        return ["any", *shown]
    terms = [["!=", value, v] for v in disabled]
    if null is not None and not null.enabled:
        terms.append(["!=", value, None])
    return ["all", *terms] if terms else None


def _is_integer_field(layer, name: str) -> bool:
    kind = layer.fields().field(name).type()
    return getattr(kind, "value", kind) in _INTEGER_TYPES


def _shift_breaks(
    layer, column: str, edges: list[float], losses: LossLog
) -> list[float]:
    """Move each class edge just past its value.

    QGIS puts an edge value in the lower class and a MapLibre `step` puts it in
    the upper one, so each edge moves up by the smallest step the field holds.
    """
    integer = _is_integer_field(layer, column)
    shifted = []
    for edge in edges:
        if integer and float(edge).is_integer():
            shifted.append(int(edge) + 1)
        elif integer:
            shifted.append(edge)
        else:
            shifted.append(math.nextafter(edge, math.inf))
    if edges and _has_value(layer, column, edges):
        losses.add(
            "class_edge_shifted", "renderer.ranges", "values equal to a class edge"
        )
    return shifted


def _has_value(layer, column: str, values: list[float]) -> bool:
    listed = ", ".join(repr(v) for v in values)
    request = QgsFeatureRequest()
    request.setFilterExpression(
        f"{QgsExpression.quotedColumnRef(column)} IN ({listed})"
    )
    request.setLimit(1)
    return any(True for _ in layer.getFeatures(request))


def _graduated(layer, renderer, losses: LossLog) -> RenderedStyle:
    ranges = sorted(renderer.ranges(), key=lambda r: r.lowerValue())
    column = _class_column(layer, renderer, ranges, losses)
    if column is None:
        return _first_class(ranges, renderer, losses)
    styles = _class_styles(
        [(r.symbol(), r.label(), r.renderState()) for r in ranges],
        losses,
        "renderer.ranges",
    )
    method = _METHODS.get(renderer.classificationMethod().id(), "manual")
    edges = [r.upperValue() for r in ranges[:-1]]
    breaks = _shift_breaks(layer, column, edges, losses)
    kind = styles[0].style.kind
    colors = [entry.style.color for entry in styles]
    sizes = [entry.style.size for entry in styles]
    size_varies = kind in SIZE_PROPERTY and len(set(sizes)) > 1
    color_varies = len(set(colors)) > 1
    if size_varies and color_varies:
        losses.add("unsupported_renderer", "renderer", "graduated size+color")
    result = _base(styles[0].style)
    config: dict[str, Any] = {
        **(result.style_config or {}),
        "mode": "graduated",
        "column": column,
        "breaks": breaks,
        "colors": colors,
        "classCount": len(ranges),
        "method": method,
    }
    if size_varies and not color_varies:
        config["target"] = "radius" if kind == "marker" else "width"
        config["sizes"] = sizes
        config["sizeRange"] = [min(sizes), max(sizes)]
        result.paint[SIZE_PROPERTY[kind]] = _step(column, sizes, breaks, 0)
    else:
        result.paint[COLOR_PROPERTY[kind]] = _step(
            column, colors, breaks, FALLBACK_COLOR
        )
    result.style_config = config
    result.filter = _range_filter(column, ranges, styles)
    return result


def _range_filter(column, ranges, styles) -> list | None:
    value = ["get", column]
    terms = [
        ["!", ["all", [">=", value, r.lowerValue()], ["<=", value, r.upperValue()]]]
        for r, entry in zip(ranges, styles)
        if not entry.enabled
    ]
    return ["all", *terms] if terms else None


def _heatmap(layer, renderer, losses: LossLog) -> RenderedStyle:
    ramp = renderer.colorRamp()
    sampled = [
        ramp.color(i / (HEATMAP_SAMPLES - 1)).name() for i in range(HEATMAP_SAMPLES)
    ]
    name, reverse, error = nearest_ramp(sampled)
    if error > RAMP_MATCH_TOLERANCE:
        losses.add(
            "raster_renderer_approximated", "renderer.colorRamp", f"drawn with {name}"
        )
    radius = to_pixels(
        renderer.radius(), renderer.radiusUnit(), losses, "renderer.radius"
    )
    builder: dict[str, Any] = {"heatmapRamp": name}
    paint: dict[str, Any] = {
        "heatmap-radius": radius,
        "heatmap-color": heatmap_color_expression(name, reverse),
    }
    if reverse:
        builder["heatmapReversed"] = True
    weight = renderer.weightExpression()
    if weight:
        column = _field_name(layer, weight)
        if column is None:
            losses.add(
                "expression_not_representable", "renderer.weightExpression", weight
            )
        else:
            builder["heatmapWeightColumn"] = column
            paint["heatmap-weight"] = ["get", column]
    return RenderedStyle(paint, {}, {"render_mode": "heatmap", "builder": builder})


def _cluster(renderer, losses: LossLog) -> RenderedStyle:
    embedded = renderer.embeddedRenderer()
    if embedded is not None and embedded.type() == "singleSymbol":
        result = _single(embedded, losses)
    else:
        kind = embedded.type() if embedded is not None else "none"
        result = _unsupported(embedded, losses, f"cluster embedded {kind}")
    builder = (result.style_config or {}).get("builder", {})
    builder["clusterRadius"] = to_pixels(
        renderer.tolerance(), renderer.toleranceUnit(), losses, "renderer.tolerance"
    )
    symbol = renderer.clusterSymbol()
    if symbol is not None and symbol.symbolLayers():
        builder["clusterColor"] = symbol.color().name()
        for symbol_layer in symbol.symbolLayers():
            if symbol_layer.layerType() == "FontMarker":
                builder["clusterTextColor"] = symbol_layer.color().name()
    result.style_config = {"render_mode": "cluster", "builder": builder}
    return result


def _extruded(renderer, losses: LossLog) -> RenderedStyle:
    roof = renderer.roofColor()
    losses.add(
        "effect_dropped", "renderer", "2.5D walls, shadow and constant extrusion height"
    )
    return RenderedStyle(
        {"fill-color": roof.name(), "fill-opacity": round(roof.alphaF(), 3)}, {}, None
    )


_RENDERERS = {
    "singleSymbol": lambda layer, renderer, losses: _single(renderer, losses),
    "categorizedSymbol": _categorized,
    "graduatedSymbol": _graduated,
    "heatmapRenderer": _heatmap,
    "pointCluster": lambda layer, renderer, losses: _cluster(renderer, losses),
    "25dRenderer": lambda layer, renderer, losses: _extruded(renderer, losses),
}


def convert_renderer(layer, losses: LossLog) -> RenderedStyle:
    """Convert a vector layer's renderer, reporting what GeoLens can't draw."""
    renderer = layer.renderer()
    kind = renderer.type() if renderer is not None else "none"
    convert = _RENDERERS.get(kind)
    if convert is None:
        return _unsupported(renderer, losses, kind)
    style = convert(layer, renderer, losses)
    if renderer.usingSymbolLevels():
        losses.add("effect_dropped", "renderer", "symbol levels")
    effect = renderer.paintEffect()
    if effect is not None and effect.enabled():
        losses.add("effect_dropped", "renderer", "draw effect")
    return style
