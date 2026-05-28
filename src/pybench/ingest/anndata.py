from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from ..bundle import BundleHandle
from ..ingest_bundle import attach_matrix, attach_metadata, define_cells
from ..spaces_write import add_space


class AnnDataIngestError(RuntimeError):
    pass


def open_anndata(source: str | Path):
    try:
        import anndata as ad
    except ImportError as e:  # pragma: no cover
        raise AnnDataIngestError("anndata is required for AnnData ingest") from e

    src = Path(source).expanduser().resolve()
    if not src.exists():
        raise AnnDataIngestError(f"AnnData source does not exist: {src}")
    return ad.read_h5ad(src)


def _obs_frame(ad) -> pd.DataFrame:
    obs = ad.obs.copy()
    obs.index = obs.index.astype(str)
    obs.insert(0, "cell_id", obs.index.astype(str))
    return obs.reset_index(drop=True)


def _obsm_keys(ad) -> list[str]:
    try:
        return [str(k) for k in ad.obsm_keys()]
    except Exception:
        return [str(k) for k in ad.obsm.keys()]


def _space_df_from_obsm(ad, key: str) -> pd.DataFrame:
    if key not in ad.obsm:
        raise AnnDataIngestError(f"obsm key not found: {key}")
    arr = ad.obsm[key]
    if getattr(arr, "shape", None) is None or len(arr.shape) != 2:
        raise AnnDataIngestError(f"obsm[{key}] is not 2D")
    n_cells, dims = int(arr.shape[0]), int(arr.shape[1])
    if n_cells != ad.n_obs:
        raise AnnDataIngestError(f"obsm[{key}] row count does not match obs")
    cell_ids = [str(x) for x in ad.obs_names.tolist()]
    pdf = pd.DataFrame(arr)
    pdf.insert(0, "cell_id", cell_ids)
    if dims == 2:
        pdf.columns = ["cell_id", "x", "y"]
    else:
        pdf.columns = ["cell_id"] + [f"dim_{i+1}" for i in range(dims)]
    return pdf


def _feature_ids(ad) -> list[str]:
    out = [str(x) for x in ad.var_names.tolist()]
    if not out:
        raise AnnDataIngestError("AnnData has no var_names/features")
    if len(set(out)) != len(out):
        raise AnnDataIngestError("AnnData var_names/features are not unique")
    return out


def _matrix_payload(ad, layer: str | None):
    if layer is None or str(layer) == "X":
        return ad.X
    layer = str(layer)
    if layer not in ad.layers:
        raise AnnDataIngestError(f"AnnData layer not found: {layer}")
    return ad.layers[layer]


def ingest_anndata_stage(
    bundle: BundleHandle,
    source: str | Path,
    *,
    define_cells_stage: bool = False,
    dataset_id: str = "dataset_1",
    set_name: str = "core",
    metadata_cols: list[str] | None = None,
    space_map: dict[str, str] | None = None,
    matrix_id: str | None = None,
    matrix_layer: str | None = None,
    feature_type: str = "gene",
    require_integer: bool = True,
    write_matrix: bool | None = None,
) -> dict[str, Any]:
    ad = open_anndata(source)
    obs = _obs_frame(ad)
    if metadata_cols is not None:
        keep = ["cell_id"] + [c for c in metadata_cols if c in obs.columns and c != "cell_id"]
        obs = obs[keep].copy()

    out: dict[str, Any] = {"source": str(Path(source).expanduser().resolve())}
    if define_cells_stage:
        out["cells"] = define_cells(
            bundle,
            cell_ids=obs["cell_id"].tolist(),
            dataset_id=dataset_id,
            set_name=set_name,
            metadata=obs,
            source="pybench_anndata",
        )
    else:
        out["metadata"] = attach_metadata(bundle, obs)

    use_space_map = dict(space_map or {})
    if not use_space_map:
        for key in _obsm_keys(ad):
            use_space_map[key] = key

    spaces_out: dict[str, Any] = {}
    for obsm_key, space_id in use_space_map.items():
        sdf = _space_df_from_obsm(ad, obsm_key)
        spaces_out[str(space_id)] = add_space(
            bundle,
            space_id=str(space_id),
            coords=sdf,
            description=str(space_id),
            source="pybench_anndata",
            overwrite=True,
        )
    out["spaces"] = spaces_out
    out["obsm_keys"] = _obsm_keys(ad)
    should_write_matrix = bool(write_matrix) or matrix_id is not None
    if should_write_matrix:
        if matrix_id is None:
            matrix_id = "RNA_counts"
        out["matrix"] = attach_matrix(
            bundle,
            matrix_id=str(matrix_id),
            mat=_matrix_payload(ad, matrix_layer).T,
            feature_ids=_feature_ids(ad),
            cell_ids=[str(x) for x in ad.obs_names.tolist()],
            layer="X" if matrix_layer is None else str(matrix_layer),
            feature_type=feature_type,
            description=f"Imported from AnnData {Path(source).name}",
            source="pybench_anndata",
            require_integer=require_integer,
            overwrite=True,
        )
        out["matrix_writer"] = "bpcells"
    else:
        out["matrix_writer"] = "skipped"
    return out
