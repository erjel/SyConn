# -*- coding: utf-8 -*-
# SyConn - Synaptic connectivity inference toolkit
#
# Copyright (c) 2016 - now
# Max-Planck-Institute for Medical Research, Heidelberg, Germany
# Authors: Sven Dorkenwald, Philipp Schubert, Jörgen Kornfeld

import sys

import cPickle as pkl
from syconn.reps import super_segmentation_dataset as ssd

path_storage_file = sys.argv[1]
path_out_file = sys.argv[2]

with open(path_storage_file) as f:
    args = []
    while True:
        try:
            args.append(pkl.load(f))
        except:
            break

out = ssd._export_ssv_to_knossosdataset_thread(args)

with open(path_out_file, "wb") as f:
    pkl.dump(out, f)