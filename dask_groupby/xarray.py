import itertools
from typing import TYPE_CHECKING, Dict, Iterable, Sequence, Tuple, Union

import dask
import numpy as np
import numpy_groupies as npg
import xarray as xr

from .aggregations import Aggregation, _atleast_1d
from .core import factorize_, groupby_reduce, reindex_

if TYPE_CHECKING:
    from xarray import DataArray, Dataset, GroupBy, Resample


def xarray_reduce(
    obj: Union["Dataset", "DataArray"],
    *by: Union["DataArray", Iterable[str], Iterable["DataArray"]],
    func: Union[str, Aggregation],
    expected_groups: Dict[str, Sequence] = None,
    bins=None,
    dim=None,
    split_out=1,
    fill_value=None,
    blockwise=False,
    keep_attrs: bool = True,
):

    by: Tuple["DataArray"] = tuple(obj[g] if isinstance(g, str) else g for g in by)  # type: ignore

    if len(by) > 1 and any(dask.is_dask_collection(by_) for by_ in by):
        raise ValueError("Grouping by multiple variables will call compute dask variables.")

    grouper_dims = set(itertools.chain(*tuple(g.dims for g in by)))
    obj, *by = xr.broadcast(obj, *by, exclude=set(obj.dims) - grouper_dims)

    if dim is None:
        dim = by[0].dims
    else:
        dim = _atleast_1d(dim)

    axis = tuple(range(-len(dim), 0))

    group_names = tuple(g.name for g in by)

    if len(by) > 1:
        group_idx, expected_groups, group_shape, _, _, _ = factorize_(
            tuple(g.data for g in by), expected_groups, bins
        )
        to_group = xr.DataArray(group_idx, dims=dim, coords={d: by[0][d] for d in by[0].indexes})
    else:
        if expected_groups is None and isinstance(by[0].data, np.ndarray):
            expected_groups = (np.unique(by[0].data),)
        if expected_groups is None:
            raise NotImplementedError(
                "Please provided expected_groups if not grouping by a numpy-backed DataArray"
            )
        group_shape = (len(expected_groups[0]),)
        to_group = by[0]

    group_sizes = dict(zip(group_names, group_shape))
    otherdims = tuple(d for d in obj.dims if d not in dim)
    indims = otherdims + dim
    result_dims = otherdims + group_names

    def wrapper(*args, **kwargs):
        result, groups = groupby_reduce(*args, **kwargs)
        if len(by) > 1:
            # all groups need not be present. reindex here
            # TODO: add test
            reindexed = reindex_(
                result,
                from_=groups,
                to=np.arange(np.prod(group_shape)),
                fill_value=fill_value,
                axis=-1,
            )
            result = reindexed.reshape(result.shape[:-1] + group_shape)
        return result

    actual = xr.apply_ufunc(
        wrapper,
        obj,
        to_group,
        input_core_dims=[indims, dim],
        dask="allowed",
        output_core_dims=[result_dims],
        dask_gufunc_kwargs=dict(output_sizes=group_sizes),
        keep_attrs=keep_attrs,
        kwargs={
            "func": func,
            "axis": axis,
            "split_out": split_out,
            "fill_value": fill_value,
            "blockwise": blockwise,
        },
    )

    for name, expect in zip(group_names, expected_groups):
        actual[name] = expect

    return actual


def xarray_groupby_reduce(
    groupby: "GroupBy",
    func: Union[str, Aggregation],
    split_out=1,
    blockwise=False,
    keep_attrs: bool = True,
):
    """Apply on an existing Xarray groupby object for convenience."""

    def wrapper(*args, **kwargs):
        result, _ = groupby_reduce(*args, **kwargs)
        return result

    groups = list(groupby.groups.keys())
    outdim = groupby._unique_coord.name
    groupdim = groupby._group_dim

    actual = xr.apply_ufunc(
        wrapper,
        groupby._obj,
        groupby._group,
        input_core_dims=[[groupdim], [groupdim]],
        dask="allowed",
        output_core_dims=[[outdim]],
        dask_gufunc_kwargs=dict(output_sizes={outdim: len(groups)}),
        keep_attrs=keep_attrs,
        kwargs={
            "func": func,
            "axis": -1,
            "split_out": split_out,
            "expected_groups": groups,
            "blockwise": blockwise,
        },
    )
    actual[outdim] = groups

    return actual


def _get_optimal_chunks_for_groups(chunks, labels):
    chunkidx = np.cumsum(chunks) - 1
    # what are the groups at chunk boundaries
    labels_at_chunk_bounds = np.unique(labels[chunkidx])
    # what's the last index of all groups
    last_indexes = npg.aggregate_numpy.aggregate(labels, np.arange(len(labels)), func="last")
    # what's the last index of groups at the chunk boundaries.
    lastidx = last_indexes[labels_at_chunk_bounds]

    if len(chunkidx) == len(lastidx) and (chunkidx == lastidx).all():
        return chunks

    first_indexes = npg.aggregate_numpy.aggregate(labels, np.arange(len(labels)), func="first")
    firstidx = first_indexes[labels_at_chunk_bounds]

    newchunkidx = [0]
    for c, f, l in zip(chunkidx, firstidx, lastidx):
        Δf = abs(c - f)
        Δl = abs(c - l)
        if c == 0 or newchunkidx[-1] > l:
            continue
        if Δf < Δl and f > newchunkidx[-1]:
            newchunkidx.append(f)
        else:
            newchunkidx.append(l + 1)
    if newchunkidx[-1] != chunkidx[-1] + 1:
        newchunkidx.append(chunkidx[-1] + 1)
    newchunks = np.diff(newchunkidx)

    assert sum(newchunks) == sum(chunks)
    return tuple(newchunks)


def rechunk_to_group_boundaries(array, dim, labels):
    """
    Rechunks array so that group boundaries line up with chunk boundaries, allowing
    parallel group reductions.

    This only works when the groups are sequential (e.g. labels = [0,0,0,1,1,1,1,2,2]).
    Such patterns occur when using ``.resample``.
    """
    axis = array.get_axis_num(dim)
    chunks = array.chunks[axis]
    newchunks = _get_optimal_chunks_for_groups(chunks, labels.data)
    if newchunks == chunks:
        return array
    else:
        return array.chunk({dim: newchunks})


def resample_reduce(
    resampler: "Resample",
    func,
    keep_attrs: bool = True,
):

    assert isinstance(resampler._obj, xr.DataArray)

    array = resampler._obj
    dim = resampler._group_dim

    # this creates a label DataArray since resample doesn't do that somehow
    tostack = []
    for idx, slicer in enumerate(resampler._group_indices):
        if slicer.stop is None:
            stop = resampler._obj.sizes[resampler._group_dim]
        else:
            stop = slicer.stop
        tostack.append(idx * np.ones((stop - slicer.start,), dtype=np.int32))
    by = xr.DataArray(np.hstack(tostack), dims=(dim,), name="__resample_dim__")

    if resampler._obj.chunks is not None:
        array = rechunk_to_group_boundaries(array, dim, by)

    result = xarray_reduce(
        array,
        by,
        func=func,
        blockwise=True,
        expected_groups=(resampler._unique_coord.data,),
        keep_attrs=keep_attrs,
    ).rename({"__resample_dim__": dim})
    return result
