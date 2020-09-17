from typing import Optional, Union, Dict

import numpy as np
from scipy import ndimage
import kimimaro
import networkx as nx
import cloudvolume
from syconn.extraction.block_processing_C import relabel_vol_nonexist2zero
from syconn.reps.super_segmentation import SuperSegmentationDataset
from syconn.reps.super_segmentation_helper import stitch_skel_nx
from syconn.handler.basics import load_pkl2obj, kd_factory
from syconn import global_params


def kimimaro_skelgen(cube_size, cube_offset, nb_cpus: Optional[int] = None,
                     ds: Optional[np.ndarray] = None) -> Dict[int, cloudvolume.Skeleton]:
    """
    code from https://pypi.org/project/kimimaro/

    Args:
        cube_size: size of processed cube in mag 1 voxels.
        cube_offset: starting point of cubes (in mag 1 voxel coordinates)
        nb_cpus: Number of cpus used by kimimaro.
        ds: Downsampling.

    Returns:
        Skeleton with nodes, edges in physical parameters

    """
    if nb_cpus is None:
        nb_cpus = 1

    ssd = SuperSegmentationDataset(working_dir=global_params.config.working_dir)
    kd = kd_factory(global_params.config.kd_seg_path)
    # TODO: uint32 conversion should be controlled externally
    seg = kd.load_seg(size=cube_size, offset=cube_offset, mag=1).swapaxes(0, 2).astype(np.uint32)
    if ds is not None:
        seg = ndimage.zoom(seg, 1 / ds, order=0)
    else:
        ds = np.ones(3)
    # transform SV IDs to agglomerated SV (SSV) IDs
    relabel_vol_nonexist2zero(seg, ssd.mapping_dict_reversed)

    # kimimaro code
    skels = kimimaro.skeletonize(
        seg,
        teasar_params={
            'scale': 2,
            'const': 500,  # physical units
            'pdrf_exponent': 4,
            'pdrf_scale': 100000,
            'soma_detection_threshold': 1100,  # physical units
            'soma_acceptance_threshold': 3500,  # physical units
            'soma_invalidation_scale': 1.0,
            'soma_invalidation_const': 2000,  # physical units
            'max_paths': 100,  # default None
        },
        dust_threshold=1000,  # skip connected components with fewer than this many voxels
        anisotropy=kd.scales[0] * ds,  # index 0 is mag 1
        fix_branching=True,  # default True
        fix_borders=True,  # default True
        fill_holes=True,
        progress=False,  # show progress bar
        parallel=nb_cpus,  # <= 0 all cpu, 1 single process, 2+ multiprocess
    )
    for cell_id in skels:
        # cell.vertices already in physical coordinates (nm)
        # now add the offset in physical coordinates
        skel = skels[cell_id]
        # cloud_volume docu: " reduce size of skeleton by factor of 50, preserves branch and end
        # points" link:https://github.com/seung-lab/cloud-volume/wiki/Advanced-Topic:-Skeleton
        skel = skel.downsample(10)
        skel = sparsify_skelcv(skel, scale=np.array([1, 1, 1]))
        skel.vertices += (cube_offset * kd.scales[0]).astype(np.int)
        skels[cell_id] = skel
    return skels


def kimimaro_mergeskels(path_list: str, cell_id: int, nb_cpus: bool = None) -> cloudvolume.Skeleton:
    """
    For debugging. Load files and merge dictionaries.

    Args:
        path_list: list of paths to locations for partial skeletons generated by kimimaro
        cell_id: ssv.ids
        nb_cpus: Number of cpus used in query of cKDTree for during stitching.

    Returns: merged skeletons with nodes in physical parameters

    """
    if nb_cpus is None:
        nb_cpus = 1

    skel_list = []
    for f in path_list:
        part_dict = load_pkl2obj(f)
        # part_dict is now a defaultdict(list)
        skel_list.extend(part_dict[int(cell_id)])
    skel = cloudvolume.PrecomputedSkeleton.simple_merge(skel_list).consolidate()
    if skel.vertices.size == 0:
        return skel
    # convert cloud volume skeleton to networkx graph
    skel = skelcv2nxgraph(skel)
    # Fuse all remaining components into a single skeleton and convert it back to cloud volume skeleton
    skel = nxgraph2skelcv(stitch_skel_nx(skel, n_jobs=nb_cpus))
    # remove small stubs and single connected components with less than 500 nodes. The latter is not applicable as
    # `stitch_skel_nx` merges all connected components regardless of their distance.
    # TODO: kimimaro.postprocess should probably be executed before `stitch_skel_nx` to remove "dust" - requires
    #  performance monitoring in large, "branchy" neurons and astrocytes.
    skel_post = kimimaro.postprocess(
        skel,
        dust_threshold=500,  # physical units
        tick_threshold=1000  # physical units
    )
    if skel_post.vertices.size == 0 and skel.vertices.size != 0:
        skel_post = skel
    # `kimimaro.postprocess` does not guarantee to return a single connected component (?!), merge them again..
    if skel_post.vertices.size > 0:
        skel_post = nxgraph2skelcv(stitch_skel_nx(skelcv2nxgraph(skel_post)))
    return skel_post


def skelcv2nxgraph(skel: cloudvolume.Skeleton) -> nx.Graph:
    """
    Transform skeleton (cloud volume) to networkx graph with node attributes 'position' and 'radius' taken from
    skel.vertices and skel.radii respectively.

    Args:
        skel:

    Returns:

    """
    g = nx.Graph()
    if skel.vertices.size == 0:
        return g
    g.add_nodes_from([(ix, dict(position=coord, radius=skel.radii[ix])) for ix, coord in enumerate(skel.vertices)])
    g.add_edges_from(skel.edges)
    return g


def nxgraph2skelcv(g: nx.Graph, radius_key: str = 'radius') -> cloudvolume.Skeleton:
    # transform networkx node IDs (non-consecutive) into a consecutive ID space
    old2new_ixs = dict()
    for ii, n in enumerate(g.nodes()):
        old2new_ixs[n] = ii
    if g.number_of_edges() == 1:
        edges = np.array(list(g.edges()), dtype=np.int)
    else:
        edges = np.array(g.edges(), dtype=np.int)
    for ii in range(edges.shape[0]):
        e1, e2 = edges[ii]
        edges[ii] = (old2new_ixs[e1], old2new_ixs[e2])
    skel = cloudvolume.Skeleton(np.array([g.node[ix]['position'] for ix in g.nodes()], dtype=np.float32),
                                edges, np.array([g.node[ix][radius_key] for ix in g.nodes()], dtype=np.float32))
    return skel


def sparsify_skelcv(skel: cloudvolume.Skeleton, scale: Optional[np.ndarray] = None,
                    angle_thresh: float = 135,
                    max_dist_thresh: Union[int, float] = 500,
                    min_dist_thresh: Union[int, float] = 50) -> cloudvolume.Skeleton:
    """
    Recursively removes nodes in skeleton. Ignores leaf and branch nodes.

    Args:
        skel: networkx graph of the sso skeleton. Requires 'position' attribute.
        scale: Scale factor; equal to the physical voxel size (nm).
        angle_thresh: Only remove nodes for which the angle between their adjacent edges is > angle_thresh (in degree).
        max_dist_thresh: Maximum distance desired between every node.
        min_dist_thresh: Minimum distance desired between every node.

    Returns: sso containing the sparse skeleton.

    """
    # TODO: this could be refactored to improve performance and readability..
    skel_nx = skelcv2nxgraph(skel)
    if scale is None:
        scale = global_params.config['scaling']
    change = 1
    while change > 0:
        change = 0
        visiting_nodes = list({k for k, v in dict(skel_nx.degree()).items() if v == 2})
        for visiting_node in visiting_nodes:
            neighbours = [n for n in skel_nx.neighbors(visiting_node)]
            if skel_nx.degree(visiting_node) == 2:
                left_node = neighbours[0]
                right_node = neighbours[1]
                vector_left_node = np.array(
                    [int(skel_nx.node[left_node]['position'][ix]) - int(skel_nx.node[visiting_node]['position'][ix]) for
                     ix in range(3)]) * scale
                vector_right_node = np.array([int(skel_nx.node[right_node]['position'][ix]) -
                                              int(skel_nx.node[visiting_node]['position'][ix]) for ix in
                                              range(3)]) * scale

                dot_prod = np.dot(vector_left_node / np.linalg.norm(vector_left_node),
                                  vector_right_node / np.linalg.norm(vector_right_node))
                angle = np.arccos(np.clip(dot_prod, -1.0, 1.0)) * 180 / np.pi
                dist = np.linalg.norm([int(skel_nx.node[right_node]['position'][ix] * scale[ix]) - int(
                    skel_nx.node[left_node]['position'][ix] * scale[ix]) for ix in range(3)])

                if (abs(angle) > angle_thresh and dist < max_dist_thresh) or dist <= min_dist_thresh:
                    skel_nx.remove_node(visiting_node)
                    skel_nx.add_edge(left_node, right_node)
                    change += 1
    return nxgraph2skelcv(skel_nx)
