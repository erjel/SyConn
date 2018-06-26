try:
    import cPickle as pkl
# TODO: switch to Python3 at some point and remove above
except Exception:
    import pickle as pkl
import glob
import numpy as np
import os
from collections import Counter
from ..mp import qsub_utils as qu
from ..mp import shared_mem as sm
script_folder = os.path.abspath(os.path.dirname(__file__) + "/../QSUB_scripts/")
from ..reps import segmentation, super_segmentation


def aggregate_segmentation_object_mappings(ssd, obj_types,
                                           stride=1000, qsub_pe=None,
                                           qsub_queue=None, nb_cpus=1):
    for obj_type in obj_types:
        assert obj_type in ssd.version_dict
    assert "sv" in ssd.version_dict

    multi_params = []
    for ssv_id_block in [ssd.ssv_ids[i:i + stride]
                         for i in
                         range(0, len(ssd.ssv_ids), stride)]:
        multi_params.append([ssv_id_block, ssd.version, ssd.version_dict,
                             ssd.working_dir, obj_types, ssd.type])

    if qsub_pe is None and qsub_queue is None:
        results = sm.start_multiprocess(
            _aggregate_segmentation_object_mappings_thread,
            multi_params, nb_cpus=nb_cpus)

    elif qu.__QSUB__:
        path_to_out = qu.QSUB_script(multi_params,
                                     "aggregate_segmentation_object_mappings",
                                     pe=qsub_pe, queue=qsub_queue,
                                     script_folder=script_folder)

    else:
        raise Exception("QSUB not available")


def _aggregate_segmentation_object_mappings_thread(args):
    ssv_obj_ids = args[0]
    version = args[1]
    version_dict = args[2]
    working_dir = args[3]
    obj_types = args[4]
    ssd_type = args[5]

    ssd = super_segmentation.SuperSegmentationDataset(working_dir, version,
                                                      ssd_type=ssd_type,
                                                      version_dict=version_dict)
    ssd.load_mapping_dict()

    for ssv_id in ssv_obj_ids:
        ssv = ssd.get_super_segmentation_object(ssv_id, True)
        mappings = dict((obj_type, Counter()) for obj_type in obj_types)

        for sv in ssv.svs:
            sv.load_attr_dict()
            for obj_type in obj_types:
                if "mapping_%s_ids" % obj_type in sv.attr_dict:
                    keys = sv.attr_dict["mapping_%s_ids" % obj_type]
                    values = sv.attr_dict["mapping_%s_ratios" % obj_type]
                    mappings[obj_type] += Counter(dict(zip(keys, values)))

        ssv.load_attr_dict()
        for obj_type in obj_types:
            if obj_type in mappings:
                ssv.attr_dict["mapping_%s_ids" % obj_type] = \
                    mappings[obj_type].keys()
                ssv.attr_dict["mapping_%s_ratios" % obj_type] = \
                    mappings[obj_type].values()

        ssv.save_attr_dict()


def apply_mapping_decisions(ssd, obj_types, stride=1000, qsub_pe=None,
                            qsub_queue=None, nb_cpus=1):
    for obj_type in obj_types:
        assert obj_type in ssd.version_dict

    multi_params = []
    for ssv_id_block in [ssd.ssv_ids[i:i + stride]
                         for i in
                         range(0, len(ssd.ssv_ids), stride)]:
        multi_params.append([ssv_id_block, ssd.version, ssd.version_dict,
                             ssd.working_dir, obj_types, ssd.type])

    if qsub_pe is None and qsub_queue is None:
        results = sm.start_multiprocess(_apply_mapping_decisions_thread,
                                        multi_params, nb_cpus=nb_cpus)

    elif qu.__QSUB__:
        path_to_out = qu.QSUB_script(multi_params,
                                     "apply_mapping_decisions",
                                     pe=qsub_pe, queue=qsub_queue,
                                     script_folder=script_folder)

    else:
        raise Exception("QSUB not available")


def _apply_mapping_decisions_thread(args):
    ssv_obj_ids = args[0]
    version = args[1]
    version_dict = args[2]
    working_dir = args[3]
    obj_types = args[4]
    ssd_type = args[5]

    ssd = super_segmentation.SuperSegmentationDataset(working_dir, version,
                                                      ssd_type=ssd_type,
                                                      version_dict=version_dict)
    ssd.load_mapping_dict()

    for ssv_id in ssv_obj_ids:
        ssv = ssd.get_super_segmentation_object(ssv_id, True)
        ssv.load_attr_dict()

        for obj_type in obj_types:

            lower_ratio = None
            upper_ratio = None
            sizethreshold = None

            if obj_type == "sj":
                correct_for_background = True
            else:
                correct_for_background = False

            assert obj_type in ssv.version_dict

            if not "mapping_%s_ratios" % obj_type in ssv.attr_dict:
                print("No mapping ratios found")
                continue

            if not "mapping_%s_ids" % obj_type in ssv.attr_dict:
                print("no mapping ids found")
                continue

            if lower_ratio is None:
                try:
                    lower_ratio = ssv.config.entries["LowerMappingRatios"][
                        obj_type]
                except:
                    raise ("Lower ratio undefined")

            if upper_ratio is None:
                try:
                    upper_ratio = ssv.config.entries["UpperMappingRatios"][
                        obj_type]
                except:
                    print("Upper ratio undefined - 1. assumed")
                    upper_ratio = 1.

            if sizethreshold is None:
                try:
                    sizethreshold = ssv.config.entries["Sizethresholds"][
                        obj_type]
                except:
                    raise ("Size threshold undefined")

            obj_ratios = np.array(ssv.attr_dict["mapping_%s_ratios" % obj_type])

            if correct_for_background:
                for i_so_id in range(
                        len(ssv.attr_dict["mapping_%s_ids" % obj_type])):
                    so_id = ssv.attr_dict["mapping_%s_ids" % obj_type][i_so_id]
                    obj_version = ssv.config.entries["Versions"][obj_type]
                    this_so = segmentation.SegmentationObject(
                        so_id, obj_type,
                        version=obj_version,
                        scaling=ssv.scaling,
                        working_dir=ssv.working_dir)
                    this_so.load_attr_dict()

                    if 0 in this_so.attr_dict["mapping_ids"]:
                        ratio_0 = this_so.attr_dict["mapping_ratios"][
                            this_so.attr_dict["mapping_ids"] == 0][0]

                        obj_ratios[i_so_id] /= (1 - ratio_0)

            id_mask = obj_ratios > lower_ratio
            if upper_ratio < 1.:
                id_mask[obj_ratios > upper_ratio] = False

            candidate_ids = \
            np.array(ssv.attr_dict["mapping_%s_ids" % obj_type])[id_mask]

            ssv.attr_dict[obj_type] = []
            for candidate_id in candidate_ids:
                obj = segmentation.SegmentationObject(candidate_id,
                                                      obj_type=obj_type,
                                                      version=
                                                      ssv.version_dict[
                                                          obj_type],
                                                      working_dir=ssv.working_dir,
                                                      config=ssv.config)
                if obj.size > sizethreshold:
                    ssv.attr_dict[obj_type].append(candidate_id)
            ssv.save_attr_dict()


def map_synaptic_conn_objects(ssd, conn_version=None, stride=1000,
                              qsub_pe=None, qsub_queue=None, nb_cpus=1,
                              n_max_co_processes=100):

    multi_params = []
    for ssv_id_block in [ssd.ssv_ids[i:i + stride]
                         for i in range(0, len(ssd.ssv_ids), stride)]:
        multi_params.append([ssv_id_block, ssd.version, ssd.version_dict,
                             ssd.working_dir, ssd.type, conn_version])

    if qsub_pe is None and qsub_queue is None:
        results = sm.start_multiprocess(
            _map_synaptic_conn_objects_thread,
            multi_params, nb_cpus=nb_cpus)

    elif qu.__QSUB__:
        path_to_out = qu.QSUB_script(multi_params,
                                     "map_synaptic_conn_objects",
                                     pe=qsub_pe, queue=qsub_queue,
                                     script_folder=script_folder,
                                     n_max_co_processes=n_max_co_processes)

    else:
        raise Exception("QSUB not available")


def _map_synaptic_conn_objects_thread(args):
    ssv_obj_ids, version, version_dict, working_dir, \
        ssd_type, conn_version = args

    ssd = super_segmentation.SuperSegmentationDataset(working_dir, version,
                                                      ssd_type=ssd_type,
                                                      version_dict=version_dict)

    conn_sd = segmentation.SegmentationDataset(obj_type="conn",
                                               working_dir=working_dir,
                                               version=conn_version)

    ssv_partners = conn_sd.load_cached_data("ssv_partners")
    syn_prob = conn_sd.load_cached_data("syn_prob")
    conn_ids = conn_sd.load_cached_data("id")

    conn_ids = conn_ids[syn_prob > .5]
    ssv_partners = ssv_partners[syn_prob > .5]

    for ssv_id in ssv_obj_ids:
        ssv = ssd.get_super_segmentation_object(ssv_id, False)
        ssv.load_attr_dict()

        ssv_conn_ids = conn_ids[np.in1d(ssv_partners[:, 0], ssv.id)]
        ssv_conn_ids = np.concatenate([ssv_conn_ids,
                                       conn_ids[np.in1d(ssv_partners[:, 1], ssv.id)]])

        ssv.attr_dict["conn_ids"] = ssv_conn_ids
        ssv.save_attr_dict()
