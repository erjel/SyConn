# -*- coding: utf-8 -*-
# SyConn - Synaptic connectivity inference toolkit
#
# Copyright (c) 2016 - now
# Max-Planck-Institute for Medical Research, Heidelberg, Germany
# Authors: Sven Dorkenwald, Philipp Schubert, Jörgen Kornfeld

import numpy as np
import os
import time
from collections import defaultdict
import cPickle as pkl
import networkx as nx
import glob
import shutil

from ..processing import objectextraction_helper as oeh
from ..processing import predictor_cnn as pc
from ..multi_proc import multi_proc_main as mpm
from ..utils import datahandler#, segmentationdataset
from syconnfs.representations import segmentation, utils

from syconnmp import qsub_utils as qu
from syconnmp import shared_mem as sm

script_folder = os.path.abspath(os.path.dirname(__file__) + "/../multi_proc/")


def correct_padding(cset, filename, offset, qsub_pe=None, qsub_queue=None):
    multi_params = []
    for chunk in cset.chunk_dict.values():
        multi_params.append([chunk, filename, offset])

    if qsub_pe is None and qsub_queue is None:
        results = mpm.start_multiprocess(pc.correct_padding_thread,
                                         multi_params, debug=False)
    elif mpm.__QSUB__:
        path_to_out = mpm.QSUB_script(multi_params,
                                      "correct_padding",
                                      pe=qsub_pe, queue=qsub_queue)
    else:
        raise Exception("QSUB not available")


def validate_chunks(cset, filename, hdf5names, qsub_pe=None, qsub_queue=None):
    multi_params = []
    for chunk in cset.chunk_dict.values():
        multi_params.append([chunk, filename, hdf5names])

    if qsub_pe is None and qsub_queue is None:
        results = mpm.start_multiprocess(oeh.validate_chunks_thread,
                                         multi_params, debug=False)
    elif mpm.__QSUB__:
        path_to_out = mpm.QSUB_script(multi_params,
                                      "validate_chunks",
                                      pe=qsub_pe, queue=qsub_queue)
    else:
        raise Exception("QSUB not available")


def validate_knossos_cubes(cset, filename, hdf5names, stride=200, qsub_pe=None, qsub_queue=None):
    coords = []
    for x in range(0, cset.box_size[0], 128):
        for y in range(0, cset.box_size[1], 128):
            for z in range(0, cset.box_size[2], 128):
                coords.append([x, y, z])

    multi_params = []
    for coord_start in xrange(0, len(coords), stride):
        multi_params.append([cset.path_head_folder, filename, hdf5names, coord_start, stride])

    if qsub_pe is None and qsub_queue is None:
        results = mpm.start_multiprocess(oeh.validate_chunks_thread,
                                         multi_params, debug=False)
    elif mpm.__QSUB__:
        path_to_out = mpm.QSUB_script(multi_params,
                                      "validate_knossos_cubes",
                                      pe=qsub_pe, queue=qsub_queue)
    else:
        raise Exception("QSUB not available")


def extract_ids(cset, filename, hdf5names, qsub_pe=None, qsub_queue=None):
    multi_params = []
    for chunk in cset.chunk_dict.values():
        multi_params.append([chunk, filename, hdf5names])

    if qsub_pe is None and qsub_queue is None:
        results = mpm.start_multiprocess(oeh.extract_ids_thread,
                                         multi_params, debug=False)
    elif mpm.__QSUB__:
        # path_to_out = mpm.QSUB_script(multi_params,
        #                               "extract_ids",
        #                               pe=qsub_pe, queue=qsub_queue)

        path_to_out = "/home/sdorkenw/QSUB/extract_ids_folder/out/"
        out_files = glob.glob(path_to_out + "/*")
        results = []
        for out_file in out_files:
            with open(out_file) as f:
                results.append(pkl.load(f))
    else:
        raise Exception("QSUB not available")

    id_mapping = {}
    for hdf5_name in hdf5names:
        id_mapping[hdf5_name] = {}
        for result in results:
            for this_id in result[1][hdf5_name]:
                if this_id in id_mapping[hdf5_name]:
                    id_mapping[hdf5_name][this_id].append(result[0])
                else:
                    id_mapping[hdf5_name][this_id] = [result[0]]

    with open(cset.path_head_folder + "/ids_" + filename + ".pkl", "w") as f:
        pkl.dump(id_mapping, f)


def calculate_chunk_numbers_for_box(cset, offset, size):
    """
    Calculates the chunk ids that are (partly) contained it the defined volume

    Parameters
    ----------
    cset : ChunkDataset
    offset : np.array
        offset of the volume to the origin
    size: np.array
        size of the volume

    Returns
    -------
    chunk_list: list
        chunk ids
    dictionary: dict
        with reverse mapping

    """

    for dim in range(3):
        offset_overlap = offset[dim] % cset.chunk_size[dim]
        offset[dim] -= offset_overlap
        size[dim] += offset_overlap
        size[dim] += (cset.chunk_size[dim] - size[dim]) % cset.chunk_size[dim]

    chunk_list = []
    translator = {}
    for x in range(offset[0], offset[0]+size[0], cset.chunk_size[0]):
        for y in range(offset[1], offset[1]+size[1], cset.chunk_size[1]):
            for z in range(offset[2], offset[2]+size[2], cset.chunk_size[2]):
                chunk_list.append(cset.coord_dict[tuple([x, y, z])])
                translator[chunk_list[-1]] = len(chunk_list)-1
    print "Chunk List contains %d elements." % len(chunk_list)
    return chunk_list, translator


def gauss_threshold_connected_components(cset, filename, hdf5names,
                                         overlap="auto", sigmas=None,
                                         thresholds=None,
                                         chunk_list=None,
                                         debug=False,
                                         swapdata=False,
                                         prob_kd_path_dict=None,
                                         membrane_filename=None,
                                         membrane_kd_path=None,
                                         hdf5_name_membrane=None,
                                         fast_load=False,
                                         suffix="",
                                         qsub_pe=None,
                                         qsub_queue=None):
    """
    Extracts connected component from probability maps
    1. Gaussian filter (defined by sigma)
    2. Thresholding (defined by threshold)
    3. Connected components analysis

    In case of vesicle clouds (hdf5_name in ["p4", "vc"]) the membrane
    segmentation is used to cut connected vesicle clouds across cells
    apart (only if membrane segmentation is provided).

    Parameters
    ----------
    cset : chunkdataset instance
    filename : str
        Filename of the prediction in the chunkdataset
    hdf5names: list of str
        List of names/ labels to be extracted and processed from the prediction
        file
    overlap: str or np.array
        Defines the overlap with neighbouring chunks that is left for later
        processing steps; if 'auto' the overlap is calculated from the sigma and
        the stitch_overlap (here: [1., 1., 1.])
    sigmas: list of lists or None
        Defines the sigmas of the gaussian filters applied to the probability
        maps. Has to be the same length as hdf5names. If None no gaussian filter
        is applied
    thresholds: list of float
        Threshold for cutting the probability map. Has to be the same length as
        hdf5names. If None zeros are used instead (not recommended!)
    chunk_list: list of int
        Selective list of chunks for which this function should work on. If None
        all chunks are used.
    debug: boolean
        If true multiprocessed steps only operate on one core using 'map' which
        allows for better error messages
    swapdata: boolean
        If true an x-z swap is applied to the data prior to processing
    label_density: np.array
        Defines the density of the data. If the data was downsampled prior to
        saving; it has to be interpolated first before processing due to
        alignment issues with the coordinate system. Two-times downsampled
        data would have a label_density of [2, 2, 2]
    membrane_filename: str
        One way to allow access to a membrane segmentation when processing
        vesicle clouds. Filename of the prediction in the chunkdataset. The
        threshold is currently set at 0.4.
    membrane_kd_path: str
        One way to allow access to a membrane segmentation when processing
        vesicle clouds. Path to the knossosdataset containing a membrane
        segmentation. The threshold is currently set at 0.4.
    hdf5_name_membrane: str
        When using the membrane_filename this key has to be given to access the
        data in the saved chunk
    fast_load: boolean
        If true the data of chunk is blindly loaded without checking for enough
        offset to compute the overlap area. Faster, because no neighbouring
        chunk has to be accessed since the default case loads th overlap area
        from them.
    suffix: str
        Suffix for the intermediate results
    qsub_pe: str or None
        qsub parallel environment
    qsub_queue: str or None
        qsub queue

    Returns
    -------
    results_as_list: list
        list containing information about the number of connected components
        in each chunk
    overlap: np.array
    stitch overlap: np.array
    """

    if thresholds is None:
        thresholds = np.zeros(len(hdf5names))
    if sigmas is None:
        sigmas = np.zeros(len(hdf5names))
    if not len(sigmas) == len(thresholds) == len(hdf5names):
        raise Exception("Number of thresholds, sigmas and HDF5 names does not "
                        "match!")

    stitch_overlap = np.array([1, 1, 1])
    if overlap == "auto":
        # Truncation of gaussian kernel is 4 per standard deviation
        # (per default). One overlap for matching of connected components
        if sigmas is None:
            max_sigma = np.zeros(3)
        else:
            max_sigma = np.array([np.max(sigmas)] * 3)

        overlap = np.ceil(max_sigma * 4) + stitch_overlap

    print "overlap:", overlap

    print "thresholds:", thresholds

    multi_params = []
    for nb_chunk in chunk_list:
        multi_params.append(
            [cset.chunk_dict[nb_chunk], cset.path_head_folder, filename,
             hdf5names, overlap,
             sigmas, thresholds, swapdata, prob_kd_path_dict,
             membrane_filename, membrane_kd_path,
             hdf5_name_membrane, fast_load, suffix])

    if qsub_pe is None and qsub_queue is None:
        results = mpm.start_multiprocess(oeh.gauss_threshold_connected_components_thread,
                                         multi_params, debug=debug)

        results_as_list = []
        for result in results:
            for entry in result:
                results_as_list.append(entry)

    elif mpm.__QSUB__:
        path_to_out = mpm.QSUB_script(multi_params,
                                      "gauss_threshold_connected_components",
                                      pe=qsub_pe, queue=qsub_queue)

        out_files = glob.glob(path_to_out + "/*")
        results_as_list = []
        for out_file in out_files:
            with open(out_file) as f:
                for entry in pkl.load(f):
                    results_as_list.append(entry)
    else:
        raise Exception("QSUB not available")

    return results_as_list, [overlap, stitch_overlap]


def make_unique_labels(cset, filename, hdf5names, chunk_list, max_nb_dict,
                       chunk_translator, debug, suffix="",
                       qsub_pe=None, qsub_queue=None):
    """
    Makes labels unique across chunks

    Parameters
    ----------
    cset : chunkdataset instance
    filename : str
        Filename of the prediction in the chunkdataset
    hdf5names: list of str
        List of names/ labels to be extracted and processed from the prediction
        file
    chunk_list: list of int
        Selective list of chunks for which this function should work on. If None
        all chunks are used.
    max_nb_dict: dictionary
        Maps each chunk id to a integer describing which needs to be added to
        all its entries
    chunk_translator: boolean
        Remapping from chunk ids to position in chunk_list
    debug: boolean
        If true multiprocessed steps only operate on one core using 'map' which
        allows for better error messages
    suffix: str
        Suffix for the intermediate results
    qsub_pe: str or None
        qsub parallel environment
    qsub_queue: str or None
        qsub queue

    """

    multi_params = []
    for nb_chunk in chunk_list:
        this_max_nb_dict = {}
        for hdf5_name in hdf5names:
            this_max_nb_dict[hdf5_name] = max_nb_dict[hdf5_name][
                chunk_translator[nb_chunk]]

        multi_params.append([cset.chunk_dict[nb_chunk], filename, hdf5names,
                             this_max_nb_dict, suffix])

    if qsub_pe is None and qsub_queue is None:
        results = mpm.start_multiprocess(oeh.make_unique_labels_thread,
                                         multi_params, debug=debug)

    elif mpm.__QSUB__:
        path_to_out = mpm.QSUB_script(multi_params,
                                      "make_unique_labels",
                                      pe=qsub_pe, queue=qsub_queue)
    else:
        raise Exception("QSUB not available")


def make_stitch_list(cset, filename, hdf5names, chunk_list, stitch_overlap,
                     overlap, debug, suffix="", qsub_pe=None, qsub_queue=None):
    """
    Creates a stitch list for the overlap region between chunks

    Parameters
    ----------
    cset : chunkdataset instance
    filename : str
        Filename of the prediction in the chunkdataset
    hdf5names: list of str
        List of names/ labels to be extracted and processed from the prediction
        file
    chunk_list: list of int
        Selective list of chunks for which this function should work on. If None
        all chunks are used.
    overlap: np.array
        Defines the overlap with neighbouring chunks that is left for later
        processing steps
    stitch_overlap: np.array
        Defines the overlap with neighbouring chunks that is left for stitching
    debug: boolean
        If true multiprocessed steps only operate on one core using 'map' which
        allows for better error messages
    suffix: str
        Suffix for the intermediate results
    qsub_pe: str or None
        qsub parallel environment
    qsub_queue: str or None
        qsub queue

    Returns:
    --------
    stitch_list: list
        list of overlapping component ids
    """

    multi_params = []
    for nb_chunk in chunk_list:
        multi_params.append([cset, nb_chunk, filename, hdf5names,
                             stitch_overlap, overlap, suffix, chunk_list])

    if qsub_pe is None and qsub_queue is None:
        results = mpm.start_multiprocess(oeh.make_stitch_list_thread,
                                         multi_params, debug=debug)

        stitch_list = {}
        for hdf5_name in hdf5names:
            stitch_list[hdf5_name] = []

        for result in results:
            for hdf5_name in hdf5names:
                elems = result[hdf5_name]
                for elem in elems:
                    stitch_list[hdf5_name].append(elem)

    elif mpm.__QSUB__:
        path_to_out = mpm.QSUB_script(multi_params,
                                      "make_stitch_list",
                                      pe=qsub_pe, queue=qsub_queue)

        out_files = glob.glob(path_to_out + "/*")

        stitch_list = {}
        for hdf5_name in hdf5names:
            stitch_list[hdf5_name] = []

        for out_file in out_files:
            with open(out_file) as f:
                result = pkl.load(f)
                for hdf5_name in hdf5names:
                    elems = result[hdf5_name]
                    for elem in elems:
                        stitch_list[hdf5_name].append(elem)
    else:
        raise Exception("QSUB not available")

    return stitch_list


def make_merge_list(hdf5names, stitch_list, max_labels):
    """
    Creates a merge list from a stitch list by mapping all connected ids to
    one id

    Parameters
    ----------
    hdf5names: list of str
        List of names/ labels to be extracted and processed from the prediction
        file
    stitch_list: dictionary
        Contains pairs of overlapping component ids for each hdf5name
    max_labels dictionary
        Contains the number of different component ids for each hdf5name

    Returns
    -------
    merge_dict: dictionary
        mergelist for each hdf5name
    merge_list_dict: dictionary
        mergedict for each hdf5name
    """

    merge_dict = {}
    merge_list_dict = {}
    for hdf5_name in hdf5names:
        this_stitch_list = stitch_list[hdf5_name]
        max_label = max_labels[hdf5_name]
        graph = nx.from_edgelist(this_stitch_list)
        cc = nx.connected_components(graph)
        merge_dict[hdf5_name] = {}
        merge_list_dict[hdf5_name] = np.arange(max_label + 1)
        for this_cc in cc:
            this_cc = list(this_cc)
            for id in this_cc:
                merge_dict[hdf5_name][id] = this_cc[0]
                merge_list_dict[hdf5_name][id] = this_cc[0]

    return merge_dict, merge_list_dict


def apply_merge_list(cset, chunk_list, filename, hdf5names, merge_list_dict,
                     debug, suffix="", qsub_pe=None, qsub_queue=None):
    """
    Applies merge list to all chunks

    Parameters
    ----------
    cset : chunkdataset instance
    chunk_list: list of int
        Selective list of chunks for which this function should work on. If None
        all chunks are used.
    filename : str
        Filename of the prediction in the chunkdataset
    hdf5names: list of str
        List of names/ labels to be extracted and processed from the prediction
        file
    merge_list_dict: dictionary
        mergedict for each hdf5name
    debug: boolean
        If true multiprocessed steps only operate on one core using 'map' which
        allows for better error messages
    suffix: str
        Suffix for the intermediate results
    qsub_pe: str or None
        qsub parallel environment
    qsub_queue: str or None
        qsub queue
    """

    multi_params = []
    merge_list_dict_path = cset.path_head_folder + "merge_list_dict.pkl"

    f = open(merge_list_dict_path, "w")
    pkl.dump(merge_list_dict, f)
    f.close()

    for nb_chunk in chunk_list:
        multi_params.append([cset.chunk_dict[nb_chunk], filename, hdf5names,
                             merge_list_dict_path, suffix])

    # results = mpm.start_multiprocess(oeh.apply_merge_list_thread,
    #                                  multi_params, debug=debug)

    if qsub_pe is None and qsub_queue is None:
        results = mpm.start_multiprocess(oeh.apply_merge_list_thread,
                                         multi_params, debug=debug)

    elif mpm.__QSUB__:
        path_to_out = mpm.QSUB_script(multi_params,
                                      "apply_merge_list",
                                      pe=qsub_pe, queue=qsub_queue)

    else:
        raise Exception("QSUB not available")


def extract_voxels(cset, filename, hdf5names=None, overlaydataset_path=None,
                   chunk_list=None, suffix="", use_work_dir=True, qsub_pe=None,
                   qsub_queue=None, n_max_processes=None):
    """
    Extracts voxels for each component id

    Parameters
    ----------
    cset : chunkdataset instance
    filename : str
        Filename of the prediction in the chunkdataset
    hdf5names: list of str
        List of names/ labels to be extracted and processed from the prediction
        file
    chunk_list: list of int
        Selective list of chunks for which this function should work on. If None
        all chunks are used.
    debug: boolean
        If true multiprocessed steps only operate on one core using 'map' which
        allows for better error messages
    suffix: str
        Suffix for the intermediate results
    qsub_pe: str or None
        qsub parallel environment
    qsub_queue: str or None
        qsub queue

    """

    if chunk_list is None:
        chunk_list = [ii for ii in range(len(cset.chunk_dict))]

    if use_work_dir:
        workfolder = os.path.dirname(cset.path_head_folder.rstrip("/"))
    else:
        workfolder = cset.path_head_folder

    voxel_rel_paths = [utils.subfold_from_ix(ix) for ix in range(100000)]

    voxel_rel_paths_2stage = np.unique([utils.subfold_from_ix(ix)[:-2]
                                        for ix in range(100000)])

    for hdf5_name in hdf5names:
        dataset_path = workfolder + "/%s_temp" % hdf5_name
        if os.path.exists(dataset_path):
            shutil.rmtree(dataset_path)

        for p in voxel_rel_paths_2stage:
            os.makedirs(dataset_path + p)

    multi_params = []
    block_steps = np.linspace(0, len(voxel_rel_paths), len(chunk_list)+1).astype(np.int)

    for nb_chunk in chunk_list:
        multi_params.append([cset.chunk_dict[nb_chunk], workfolder,
                             filename, hdf5names, overlaydataset_path,
                             suffix,
                             voxel_rel_paths[block_steps[nb_chunk]:
                                             block_steps[nb_chunk+1]]])

    if qsub_pe is None and qsub_queue is None:
        results = sm.start_multiprocess(oeh.extract_voxels_thread,
                                        multi_params, nb_cpus=1)

    elif qu.__QSUB__:
        path_to_out = qu.QSUB_script(multi_params,
                                     "extract_voxels",
                                     pe=qsub_pe, queue=qsub_queue,
                                     script_folder=script_folder,
                                     n_max_co_processes=n_max_processes)

        # path_to_out = "/u/sdorkenw/QSUB/extract_voxels_folder/out/"
        out_files = glob.glob(path_to_out + "/*")
        results = []
        for out_file in out_files:
            with open(out_file) as f:
                results.append(pkl.load(f))

    else:
        raise Exception("QSUB not available")

    for hdf5_name in hdf5names:

        remap_dict = defaultdict(list)
        for result in results:
            for key, value in result[hdf5_name].iteritems():
                remap_dict[key].append(value)

        with open(workfolder + "/%s_temp/remapping_dict.pkl" % hdf5_name, "w") as f:
            pkl.dump(remap_dict, f)


def combine_voxels(workfolder, hdf5names=None, stride=100, qsub_pe=None,
                   qsub_queue=None, n_max_processes=None):
    """
    Extracts voxels for each component id

    Parameters
    ----------
    cset : chunkdataset instance
    filename : str
        Filename of the prediction in the chunkdataset
    hdf5names: list of str
        List of names/ labels to be extracted and processed from the prediction
        file
    chunk_list: list of int
        Selective list of chunks for which this function should work on. If None
        all chunks are used.
    debug: boolean
        If true multiprocessed steps only operate on one core using 'map' which
        allows for better error messages
    suffix: str
        Suffix for the intermediate results
    qsub_pe: str or None
        qsub parallel environment
    qsub_queue: str or None
        qsub queue

    """
    voxel_rel_paths_2stage = np.unique([utils.subfold_from_ix(ix)[:-2]
                                        for ix in range(100000)])

    dataset_versions = {}
    for hdf5_name in hdf5names:
        segdataset = segmentation.SegmentationDataset(obj_type=hdf5_name,
                                                      working_dir=workfolder,
                                                      version="new",
                                                      create=True)
        dataset_versions[hdf5_name] = segdataset.version

        for p in voxel_rel_paths_2stage:
            os.makedirs(segdataset.so_storage_path + p)

    multi_params = []

    for hdf5_name in hdf5names:
        with open(workfolder + "/%s_temp/remapping_dict.pkl" % hdf5_name, "r") as f:
            remap_dict = pkl.load(f)

        so_ids = remap_dict.keys()

        so_id_dict = defaultdict(list)
        for so_id in so_ids:
            so_id_str = "%.5d" % so_id
            so_id_dict[so_id_str[-5:]].append(so_id)

        for so_id_block in [so_id_dict.values()[i:i + stride]
                            for i in xrange(0, len(so_id_dict), stride)]:

            multi_params.append([workfolder, hdf5_name, so_id_block,
                                 dataset_versions[hdf5_name]])

    if qsub_pe is None and qsub_queue is None:
        results = sm.start_multiprocess(oeh.combine_voxels_thread,
                                        multi_params, nb_cpus=1)

    elif qu.__QSUB__:
        path_to_out = qu.QSUB_script(multi_params,
                                     "combine_voxels",
                                     pe=qsub_pe, queue=qsub_queue,
                                     script_folder=script_folder,
                                     n_max_co_processes=n_max_processes)

    else:
        raise Exception("QSUB not available")


def from_probabilities_to_objects(cset, filename, hdf5names,
                                  overlap="auto", sigmas=None,
                                  thresholds=None,
                                  chunk_list=None,
                                  debug=False,
                                  swapdata=0,
                                  offset=None,
                                  size=None,
                                  prob_kd_path_dict=None,
                                  membrane_filename=None,
                                  membrane_kd_path=None,
                                  hdf5_name_membrane=None,
                                  suffix="",
                                  qsub_pe=None,
                                  qsub_queue=None):
    """
    Main function for the object extraction step; combines all needed steps

    Parameters
    ----------
    cset : chunkdataset instance
    filename : str
        Filename of the prediction in the chunkdataset
    hdf5names: list of str
        List of names/ labels to be extracted and processed from the prediction
        file
    overlap: str or np.array
        Defines the overlap with neighbouring chunks that is left for later
        processing steps; if 'auto' the overlap is calculated from the sigma and
        the stitch_overlap (here: [1., 1., 1.])
    sigmas: list of lists or None
        Defines the sigmas of the gaussian filters applied to the probability
        maps. Has to be the same length as hdf5names. If None no gaussian filter
        is applied
    thresholds: list of float
        Threshold for cutting the probability map. Has to be the same length as
        hdf5names. If None zeros are used instead (not recommended!)
    chunk_list: list of int
        Selective list of chunks for which this function should work on. If None
        all chunks are used.
    debug: boolean
        If true multiprocessed steps only operate on one core using 'map' which
        allows for better error messages
    swapdata: boolean
        If true an x-z swap is applied to the data prior to processing
    label_density: np.array
        Defines the density of the data. If the data was downsampled prior to
        saving; it has to be interpolated first before processing due to
        alignment issues with the coordinate system. Two-times downsampled
        data would have a label_density of [2, 2, 2]
    offset : np.array
        offset of the volume to the origin
    size: np.array
        size of the volume
    membrane_filename: str
        One way to allow access to a membrane segmentation when processing
        vesicle clouds. Filename of the prediction in the chunkdataset. The
        threshold is currently set at 0.4.
    membrane_kd_path: str
        One way to allow access to a membrane segmentation when processing
        vesicle clouds. Path to the knossosdataset containing a membrane
        segmentation. The threshold is currently set at 0.4.
    hdf5_name_membrane: str
        When using the membrane_filename this key has to be given to access the
        data in the saved chunk
    suffix: str
        Suffix for the intermediate results
    qsub_pe: str or None
        qsub parallel environment
    qsub_queue: str or None
        qsub queue

    """
    all_times = []
    step_names = []

    if prob_kd_path_dict is not None:
        kd_keys = prob_kd_path_dict.keys()
        assert len(kd_keys) == len(hdf5names)
        for kd_key in kd_keys:
            assert kd_key in hdf5names

    if size is not None and offset is not None:
        chunk_list, chunk_translator = \
            calculate_chunk_numbers_for_box(cset, offset, size)
    else:
        chunk_translator = {}
        if chunk_list is None:
            chunk_list = [ii for ii in range(len(cset.chunk_dict))]
            for ii in range(len(cset.chunk_dict)):
                chunk_translator[ii] = ii
        else:
            for ii in range(len(chunk_list)):
                chunk_translator[chunk_list[ii]] = ii

    if thresholds is not None and thresholds[0] <= 1.:
        thresholds = np.array(thresholds)
        thresholds *= 255

    if sigmas is not None and swapdata == 1:
        for nb_sigma in range(len(sigmas)):
            if len(sigmas[nb_sigma]) == 3:
                sigmas[nb_sigma] = \
                    datahandler.switch_array_entries(sigmas[nb_sigma], [0, 2])

    # --------------------------------------------------------------------------

    time_start = time.time()
    cc_info_list, overlap_info = gauss_threshold_connected_components(
        cset, filename,
        hdf5names, overlap, sigmas, thresholds,
        chunk_list, debug,
        swapdata,
        prob_kd_path_dict=prob_kd_path_dict,
        membrane_filename=membrane_filename,
        membrane_kd_path=membrane_kd_path,
        hdf5_name_membrane=hdf5_name_membrane,
        fast_load=True, suffix=suffix,
        qsub_pe=qsub_pe,
        qsub_queue=qsub_queue)

    stitch_overlap = overlap_info[1]
    overlap = overlap_info[0]
    all_times.append(time.time() - time_start)
    step_names.append("conneceted components")
    print "\nTime needed for connected components: %.3fs" % all_times[-1]

    # --------------------------------------------------------------------------

    time_start = time.time()
    nb_cc_dict = {}
    max_nb_dict = {}
    max_labels = {}
    for hdf5_name in hdf5names:
        nb_cc_dict[hdf5_name] = np.zeros(len(chunk_list), dtype=np.int32)
        max_nb_dict[hdf5_name] = np.zeros(len(chunk_list), dtype=np.int32)
    for cc_info in cc_info_list:
        nb_cc_dict[cc_info[1]][chunk_translator[cc_info[0]]] = cc_info[2]
    for hdf5_name in hdf5names:
        max_nb_dict[hdf5_name][0] = 0
        for nb_chunk in range(1, len(chunk_list)):
            max_nb_dict[hdf5_name][nb_chunk] = \
                max_nb_dict[hdf5_name][nb_chunk - 1] + \
                nb_cc_dict[hdf5_name][nb_chunk - 1]
        max_labels[hdf5_name] = int(max_nb_dict[hdf5_name][-1] + \
                                    nb_cc_dict[hdf5_name][-1])
    all_times.append(time.time() - time_start)
    step_names.append("extracting max labels")
    print "\nTime needed for extracting max labels: %.6fs" % all_times[-1]
    print "Max labels: ", max_labels

    # --------------------------------------------------------------------------

    time_start = time.time()
    make_unique_labels(cset, filename, hdf5names, chunk_list, max_nb_dict,
                       chunk_translator, debug, suffix=suffix,
                       qsub_pe=qsub_pe, qsub_queue=qsub_queue)
    all_times.append(time.time() - time_start)
    step_names.append("unique labels")
    print "\nTime needed for unique labels: %.3fs" % all_times[-1]

    # --------------------------------------------------------------------------

    time_start = time.time()
    stitch_list = make_stitch_list(cset, filename, hdf5names, chunk_list,
                                   stitch_overlap, overlap, debug,
                                   suffix=suffix, qsub_pe=qsub_pe,
                                   qsub_queue=qsub_queue)
    all_times.append(time.time() - time_start)
    step_names.append("stitch list")
    print "\nTime needed for stitch list: %.3fs" % all_times[-1]

    # --------------------------------------------------------------------------

    time_start = time.time()
    merge_dict, merge_list_dict = make_merge_list(hdf5names, stitch_list,
                                                  max_labels)
    all_times.append(time.time() - time_start)
    step_names.append("merge list")
    print "\nTime needed for merge list: %.3fs" % all_times[-1]
    # if all_times[-1] < 0.01:
    #     raise Exception("That was too fast!")

    # -------------------------------------------------------------------------

    time_start = time.time()
    apply_merge_list(cset, chunk_list, filename, hdf5names, merge_list_dict,
                     debug, suffix=suffix, qsub_pe=qsub_pe,
                     qsub_queue=qsub_queue)
    all_times.append(time.time() - time_start)
    step_names.append("apply merge list")
    print "\nTime needed for applying merge list: %.3fs" % all_times[-1]

    # --------------------------------------------------------------------------

    time_start = time.time()
    extract_voxels(cset, filename, hdf5names, debug=debug,
                   chunk_list=chunk_list, suffix=suffix, use_work_dir=True,
                   qsub_pe=qsub_pe, qsub_queue=qsub_queue)
    all_times.append(time.time() - time_start)
    step_names.append("voxel extraction")
    print "\nTime needed for extracting voxels: %.3fs" % all_times[-1]

    # --------------------------------------------------------------------------

    print "\nTime overview:"
    for ii in range(len(all_times)):
        print "%s: %.3fs" % (step_names[ii], all_times[ii])
    print "--------------------------"
    print "Total Time: %.1f min" % (np.sum(all_times) / 60)
    print "--------------------------\n\n"


def from_probabilities_to_objects_parameter_sweeping(cset,
                                                     filename,
                                                     hdf5names,
                                                     nb_thresholds,
                                                     overlap="auto",
                                                     sigmas=None,
                                                     chunk_list=None,
                                                     swapdata=0,
                                                     label_density=np.ones(3),
                                                     offset=None,
                                                     size=None,
                                                     membrane_filename=None,
                                                     membrane_kd_path=None,
                                                     hdf5_name_membrane=None,
                                                     qsub_pe=None,
                                                     qsub_queue=None):
    """
    Sweeps over different thresholds. Each objectextraction resutls are saved in
    a seperate folder, all intermediate steps are saved with a different suffix

    Parameters
    ----------
    cset : chunkdataset instance
    filename : str
        Filename of the prediction in the chunkdataset
    hdf5names: list of str
        List of names/ labels to be extracted and processed from the prediction
        file
    nb_thresholds: integer
        number of thresholds and therefore runs of objectextractions to do;
        the actual thresholds are equally spaced
    overlap: str or np.array
        Defines the overlap with neighbouring chunks that is left for later
        processing steps; if 'auto' the overlap is calculated from the sigma and
        the stitch_overlap (here: [1., 1., 1.])
    sigmas: list of lists or None
        Defines the sigmas of the gaussian filters applied to the probability
        maps. Has to be the same length as hdf5names. If None no gaussian filter
        is applied
    chunk_list: list of int
        Selective list of chunks for which this function should work on. If None
        all chunks are used.
    swapdata: boolean
        If true an x-z swap is applied to the data prior to processing
    label_density: np.array
        Defines the density of the data. If the data was downsampled prior to
        saving; it has to be interpolated first before processing due to
        alignment issues with the coordinate system. Two-times downsampled
        data would have a label_density of [2, 2, 2]
    offset : np.array
        offset of the volume to the origin
    size: np.array
        size of the volume
    membrane_filename: str
        One way to allow access to a membrane segmentation when processing
        vesicle clouds. Filename of the prediction in the chunkdataset. The
        threshold is currently set at 0.4.
    membrane_kd_path: str
        One way to allow access to a membrane segmentation when processing
        vesicle clouds. Path to the knossosdataset containing a membrane
        segmentation. The threshold is currently set at 0.4.
    hdf5_name_membrane: str
        When using the membrane_filename this key has to be given to access the
        data in the saved chunk
    suffix: str
        Suffix for the intermediate results
    qsub_pe: str
        qsub parallel environment name
    qsub_queue: str or None
        qsub queue name
    """

    thresholds = np.array(
        255. / (nb_thresholds + 1) * np.array(range(1, nb_thresholds + 1)),
        dtype=np.uint8)

    all_times = []
    for nb, t in enumerate(thresholds):
        print "\n\n ======= t = %.2f =======" % t
        time_start = time.time()
        from_probabilities_to_objects(cset, filename, hdf5names,
                                      overlap=overlap, sigmas=sigmas,
                                      thresholds=[t] * len(hdf5names),
                                      chunk_list=chunk_list,
                                      swapdata=swapdata,
                                      label_density=label_density,
                                      offset=offset,
                                      size=size,
                                      membrane_filename=membrane_filename,
                                      membrane_kd_path=membrane_kd_path,
                                      hdf5_name_membrane=hdf5_name_membrane,
                                      suffix=str(nb),
                                      qsub_pe=qsub_pe,
                                      qsub_queue=qsub_queue,
                                      debug=False)
        all_times.append(time.time() - time_start)

    print "\n\nTime overview:"
    for ii in range(len(all_times)):
        print "t = %.2f: %.1f min" % (thresholds[ii], all_times[ii] / 60)
    print "--------------------------"
    print "Total Time: %.1f min" % (np.sum(all_times) / 60)
    print "--------------------------\n"


def from_ids_to_objects(cset, filename, hdf5names=None,
                        overlaydataset_path=None, chunk_list=None, offset=None,
                        size=None, suffix="", qsub_pe=None, qsub_queue=None,
                        n_max_processes=None):
    """
    Main function for the object extraction step; combines all needed steps

    Parameters
    ----------
    cset : chunkdataset instance
    filename : str
        Filename of the prediction in the chunkdataset
    hdf5names: list of str
        List of names/ labels to be extracted and processed from the prediction
        file
    chunk_list: list of int
        Selective list of chunks for which this function should work on. If None
        all chunks are used.
    debug: boolean
        If true multiprocessed steps only operate on one core using 'map' which
        allows for better error messages
    offset : np.array
        offset of the volume to the origin
    size: np.array
        size of the volume
    suffix: str
        Suffix for the intermediate results
    qsub_pe: str or None
        qsub parallel environment
    qsub_queue: str or None
        qsub queue

    """
    assert overlaydataset_path is not None or hdf5names is not None

    all_times = []
    step_names = []
    if size is not None and offset is not None:
        chunk_list, chunk_translator = \
            calculate_chunk_numbers_for_box(cset, offset, size)
    else:
        chunk_translator = {}
        if chunk_list is None:
            chunk_list = [ii for ii in range(len(cset.chunk_dict))]
            for ii in range(len(cset.chunk_dict)):
                chunk_translator[ii] = ii
        else:
            for ii in range(len(chunk_list)):
                chunk_translator[chunk_list[ii]] = ii

    # --------------------------------------------------------------------------

    time_start = time.time()
    extract_voxels(cset, filename, hdf5names,
                   overlaydataset_path=overlaydataset_path,
                   chunk_list=chunk_list, suffix=suffix, qsub_pe=qsub_pe,
                   qsub_queue=qsub_queue, n_max_processes=n_max_processes)
    all_times.append(time.time() - time_start)
    step_names.append("voxel extraction")
    print "\nTime needed for extracting voxels: %.3fs" % all_times[-1]

    # --------------------------------------------------------------------------

    time_start = time.time()
    combine_voxels(os.path.dirname(cset.path_head_folder.rstrip("/")),
                   hdf5names, qsub_pe=qsub_pe, qsub_queue=qsub_queue,
                   n_max_processes=n_max_processes)
    all_times.append(time.time() - time_start)
    step_names.append("combine voxels")
    print "\nTime needed for combining voxels: %.3fs" % all_times[-1]

    # --------------------------------------------------------------------------

    print "\nTime overview:"
    for ii in range(len(all_times)):
        print "%s: %.3fs" % (step_names[ii], all_times[ii])
    print "--------------------------"
    print "Total Time: %.1f min" % (np.sum(all_times) / 60)
    print "--------------------------\n\n"