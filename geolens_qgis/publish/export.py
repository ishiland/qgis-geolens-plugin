"""Export QGIS layers to the files GeoLens ingests.

``plan_export`` runs on the GUI thread and sorts layers into files to write,
layers that already live on the target server, and layers GeoLens can't take.
``ExportTask`` writes the files in the background.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from qgis.core import (
    Qgis,
    QgsRasterBlockFeedback,
    QgsRasterFileWriter,
    QgsRasterLayer,
    QgsRasterPipe,
    QgsTask,
    QgsVectorFileWriter,
)

VECTOR = "vector"
TABLE = "table"
RASTER = "raster"

# Raster providers that stream tiles from a service instead of reading data.
_SERVICE_RASTER_PROVIDERS = {"wms", "wmts", "arcgismapserver", "wcs"}
_GEOTIFF_SUFFIXES = {".tif", ".tiff"}
_UNSAFE_FILE_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    layer_id: str
    layer_name: str
    message: str


@dataclass(frozen=True)
class Reference:
    """A layer loaded from the target server, published as a dataset link."""

    layer_id: str
    layer_name: str
    dataset_id: str
    editable: bool


@dataclass(frozen=True)
class PlannedLayer:
    layer: object
    kind: str
    selected_only: bool = False


@dataclass
class ExportPlan:
    to_export: list[PlannedLayer] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


@dataclass(frozen=True)
class ExportedLayer:
    layer_id: str
    layer_name: str
    kind: str
    path: Path
    size: int
    sha256: str
    # False when the layer's own GeoTIFF is uploaded as it is.
    temporary: bool


@dataclass
class ExportResult:
    exported: list[ExportedLayer] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


class ExportError(RuntimeError):
    pass


def _server_key(url: str) -> str:
    parsed = urlparse(url.strip())
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def plan_export(
    layers: Iterable,
    server_url: str,
    selected_only: Iterable[str] = (),
) -> ExportPlan:
    """Sort `layers` into references, skips and files to export.

    `selected_only` holds the ids of vector layers to export with only their
    selected features.
    """
    plan = ExportPlan()
    selected_ids = set(selected_only)
    target = _server_key(server_url)
    for layer in layers:
        layer_id, name = layer.id(), layer.name()
        dataset_id = layer.customProperty("geolens/dataset_id")
        loaded_from = layer.customProperty("geolens/server")
        if dataset_id and loaded_from and _server_key(str(loaded_from)) == target:
            plan.references.append(
                Reference(
                    layer_id,
                    name,
                    str(dataset_id),
                    layer.customProperty("geolens/editable") is True,
                )
            )
            plan.issues.append(
                Issue(
                    "A034",
                    "info",
                    layer_id,
                    name,
                    "Loaded from this GeoLens, so the map references its dataset "
                    "and nothing is uploaded.",
                )
            )
            continue
        planned = _classify(layer, layer_id in selected_ids, plan.issues)
        if planned:
            plan.to_export.append(planned)
    return plan


def _classify(layer, selected_only: bool, issues: list[Issue]) -> PlannedLayer | None:
    layer_id, name = layer.id(), layer.name()
    layer_type = layer.type()
    if layer_type == Qgis.LayerType.Vector:
        if layer.featureCount() == 0:
            issues.append(_empty(layer_id, name, "Layer is empty and will be skipped."))
            return None
        if selected_only and layer.selectedFeatureCount() == 0:
            issues.append(
                _empty(
                    layer_id,
                    name,
                    "No features are selected, so the layer will be skipped.",
                )
            )
            return None
        kind = VECTOR if layer.isSpatial() else TABLE
        if kind == TABLE:
            issues.append(
                Issue(
                    "A032",
                    "info",
                    layer_id,
                    name,
                    "Table without geometry: published as a table, "
                    "not shown on the map.",
                )
            )
        return PlannedLayer(layer, kind, selected_only)
    if (
        layer_type == Qgis.LayerType.Raster
        and layer.providerType() not in _SERVICE_RASTER_PROVIDERS
    ):
        return PlannedLayer(layer, RASTER)
    issues.append(
        Issue(
            "A030",
            "warning",
            layer_id,
            name,
            "This layer type can't be published and will be skipped.",
        )
    )
    return None


def _empty(layer_id: str, name: str, message: str) -> Issue:
    return Issue("A031", "warning", layer_id, name, message)


def safe_file_stem(name: str) -> str:
    stem = _UNSAFE_FILE_CHARS.sub("", name).strip().strip(".")
    return stem or "layer"


def _unique_path(folder: Path, stem: str, suffix: str) -> Path:
    path = folder / f"{stem}{suffix}"
    counter = 2
    while path.exists():
        path = folder / f"{stem}_{counter}{suffix}"
        counter += 1
    return path


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_vector(planned: PlannedLayer, path: Path, transform_context, feedback):
    layer = planned.layer
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG" if planned.kind == TABLE else "FlatGeobuf"
    options.fileEncoding = "UTF-8"
    options.onlySelectedFeatures = planned.selected_only
    options.feedback = feedback
    # A GeoPackage names its table; FlatGeobuf takes the file name.
    options.layerName = path.stem
    error, message, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, str(path), transform_context, options
    )
    if error != QgsVectorFileWriter.WriterError.NoError:
        raise ExportError(f"Could not export {layer.name()}: {message}")


def _geotiff_path(layer) -> Path | None:
    if layer.providerType() != "gdal":
        return None
    path = Path(layer.source().split("|", 1)[0])
    if path.suffix.lower() in _GEOTIFF_SUFFIXES and path.is_file():
        return path
    return None


def _write_raster(layer: QgsRasterLayer, path: Path, transform_context, feedback):
    pipe = QgsRasterPipe()
    pipe.set(layer.dataProvider().clone())
    writer = QgsRasterFileWriter(str(path))
    writer.setOutputFormat("GTiff")
    error = writer.writeRaster(
        pipe,
        layer.width(),
        layer.height(),
        layer.extent(),
        layer.crs(),
        transform_context,
        feedback,
    )
    if error != Qgis.RasterFileWriterResult.Success:
        raise ExportError(f"Could not export {layer.name()}")


class ExportTask(QgsTask):
    """Writes the planned layers to `folder`.

    The layers stay locked for the task's lifetime, so a QGIS user can't
    remove or edit one mid-export. Read `result` once the task finishes; if
    `run` returned False, `error` says why.
    """

    def __init__(
        self,
        plan: ExportPlan,
        folder: str | Path | None,
        transform_context,
        max_file_size_bytes: int | None = None,
        description: str = "Export layers for GeoLens",
    ):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self.plan = plan
        self.folder = Path(folder or tempfile.mkdtemp(prefix="geolens_publish_"))
        self.transform_context = transform_context
        self.max_file_size_bytes = max_file_size_bytes
        self.feedback = QgsRasterBlockFeedback()
        self.result = ExportResult(
            references=list(plan.references), issues=list(plan.issues)
        )
        self.error = ""
        self.setDependentLayers([planned.layer for planned in plan.to_export])

    def cancel(self):
        self.feedback.cancel()
        super().cancel()

    def run(self) -> bool:
        total = max(1, len(self.plan.to_export))
        try:
            for index, planned in enumerate(self.plan.to_export):
                if self.isCanceled():
                    return False
                self.result.exported.append(self._export(planned))
                self.setProgress(100 * (index + 1) / total)
        except (ExportError, OSError) as error:
            self.error = str(error)
            return False
        return True

    def _export(self, planned: PlannedLayer) -> ExportedLayer:
        layer = planned.layer
        temporary = True
        path = None
        if planned.kind == RASTER:
            path = _geotiff_path(layer)
            temporary = path is None
        if path is None:
            suffix = {VECTOR: ".fgb", TABLE: ".gpkg", RASTER: ".tif"}[planned.kind]
            path = _unique_path(self.folder, safe_file_stem(layer.name()), suffix)
            if planned.kind == RASTER:
                _write_raster(layer, path, self.transform_context, self.feedback)
            else:
                _write_vector(planned, path, self.transform_context, self.feedback)
        size = path.stat().st_size
        if self.max_file_size_bytes is not None and size > self.max_file_size_bytes:
            self.result.issues.append(
                Issue(
                    "A010",
                    "error",
                    layer.id(),
                    layer.name(),
                    f"The exported file is {size:,} bytes, over the server's "
                    f"limit of {self.max_file_size_bytes:,}.",
                )
            )
        return ExportedLayer(
            layer.id(),
            layer.name(),
            planned.kind,
            path,
            size,
            sha256_of(path),
            temporary,
        )
