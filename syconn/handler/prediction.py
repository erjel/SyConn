import re
from .basics import read_txt_from_zip
from collections import Counter
from knossos_utils.chunky import ChunkDataset, save_dataset
from knossos_utils.knossosdataset import KnossosDataset
from elektronn2.config import config as e2config
from elektronn2.utils.gpu import initgpu
from .compression import load_from_h5py, save_to_h5py
import numpy as np
import os
import sys
import time
import warnings
import tqdm


def load_gt_from_kzip(zip_fname, kd_p, raw_data_offset=75, verbose=False):
    """
    Loads ground truth from zip file, generated with Knossos. Corresponding
    dataset config file is locatet at kd_p.

    Parameters
    ----------
    zip_fname : str
    kd_p : str
    raw_data_offset : int
        additional offset for raw data to use full label volume, i.e. raw cube shape will be the shape of the labels
        plus 2 times raw_data_offset

    Returns
    -------
    np.array, np.array
        raw data, label data
    """
    bb = parse_movement_area_from_zip(zip_fname)
    offset, size = bb[0], bb[1] - bb[0]
    kd = KnossosDataset()
    kd.initialize_from_knossos_path(kd_p)
    scaling = np.array(kd.scale, dtype=np.int)
    if np.isscalar(raw_data_offset):
        raw_data_offset = np.array(scaling[0] * raw_data_offset / scaling)
        if verbose:
            print('Using scale adapted raw offset:', np.array([75, 75,  6]))
    elif len(raw_data_offset) != 3:
        raise ValueError("Offset for raw cubes has to have length 3.")
    raw = kd.from_raw_cubes_to_matrix(size + 2 * raw_data_offset,
                                      offset - raw_data_offset, nb_threads=2,
                                      mag=1, show_progress=False)
    try:
        label = kd.from_kzip_to_matrix(zip_fname, size, offset, mag=1,
                                       verbose=False, show_progress=False)
        label = label.astype(np.uint16)
    except Exception as e:
        print("\nError occured for file " + zip_fname + repr(e) +
              "\nLabels are set to zeros (background).")
        label = np.zeros_like(raw).astype(np.uint16)
    return raw.astype(np.float32) / 255., label


def predict_kzip(kzip_p, m_path, kd_path, clf_thresh=0.5, mfp_active=False,
                 dest_path=None, overwrite=False, gpu_ix=0,
                 imposed_patch_size=None):
    """
    Predicts data contained in k.zip file (defined by bounding box in knossos)

    Parameters
    ----------
    kzip_p : str
        path to kzip containing the raw data cube information
    m_path : str
        path to predictive model
    kd_path : str
        path to knossos dataset
    clf_thresh : float
        classification threshold
    overwrite : bool
    mfp_active : False
    imposed_patch_size : tuple
    dest_path : str
        path to destination folder, if None folder of k.zip is used.
    gpu_ix : int
    """
    cube_name = os.path.splitext(os.path.basename(kzip_p))[0]
    if dest_path is None:
        dest_path = os.path.dirname(kzip_p)
    if not os.path.isfile(dest_path + "/%s_data.h5" % cube_name) or overwrite:
        raw, labels = load_gt_from_kzip(kzip_p, kd_p=kd_path,
                                        raw_data_offset=0)
        raw = xyz2zxy(raw)
        initgpu(gpu_ix)
        from elektronn2.neuromancer.model import modelload
        m = modelload(m_path, imposed_patch_size=list(imposed_patch_size)
        if isinstance(imposed_patch_size, tuple) else imposed_patch_size,
                      override_mfp_to_active=mfp_active, imposed_batch_size=1)
        original_do_rates = m.dropout_rates
        m.dropout_rates = ([0.0, ] * len(original_do_rates))
        pred = m.predict_dense(raw[None, ], pad_raw=True)[1]
        # remove area without sufficient FOV
        pred = zxy2xyz(pred)
        raw = zxy2xyz(raw)
        save_to_h5py([pred, raw], dest_path + "/%s_data.h5" % cube_name,
                     ["pred", "raw"])
    else:
        pred, raw = load_from_h5py(dest_path + "/%s_data.h5" % cube_name,
                                   hdf5_names=["pred", "raw"])
    offset = parse_movement_area_from_zip(kzip_p)[0]
    overlaycubes2kzip(dest_path + "/%s_pred.k.zip" % cube_name,
                      (pred >= clf_thresh).astype(np.uint32),
                      offset, kd_path)


def predict_h5(h5_path, m_path, clf_thresh=None, mfp_active=False,
               gpu_ix=0, imposed_patch_size=None, hdf5_data_key=None,
               data_is_zxy=True, dest_p=None, dest_hdf5_data_key="pred",
               as_uint8=True):
    """
    Predicts data from h5 file. Assumes raw data is already float32.

    Parameters
    ----------
    h5_path : str
        path to h5 containing the raw data
    m_path : str
        path to predictive model
    clf_thresh : float
        classification threshold, if None, no thresholding
    mfp_active : False
    imposed_patch_size : tuple
    gpu_ix : int
    hdf5_data_key: str
        if None, it uses the first entry in the list returned by
        'load_from_h5py'
    data_is_zxy : bool
        if False, it will assumes data is [X, Y, Z]
    as_uint8: bool
    dest_p : str
    """
    if hdf5_data_key:
        raw = load_from_h5py(h5_path, hdf5_names=[hdf5_data_key])[0]
    else:
        raw = load_from_h5py(h5_path, hdf5_names=None)
        assert len(raw) == 1, "'hdf5_data_key' not given but multiple hdf5 " \
                              "elements found. Please define raw data key."
        raw = raw[0]
    if not data_is_zxy:
        raw = xyz2zxy(raw)
    initgpu(gpu_ix)
    from elektronn2.neuromancer.model import modelload
    m = modelload(m_path, imposed_patch_size=list(imposed_patch_size)
    if isinstance(imposed_patch_size, tuple) else imposed_patch_size,
                  override_mfp_to_active=mfp_active, imposed_batch_size=1)
    original_do_rates = m.dropout_rates
    m.dropout_rates = ([0.0, ] * len(original_do_rates))
    pred = m.predict_dense(raw[None, ], pad_raw=True)[1]
    pred = zxy2xyz(pred)
    raw = zxy2xyz(raw)
    if as_uint8:
        pred = (pred * 255).astype(np.uint8)
        raw = (raw * 255).astype(np.uint8)
    if clf_thresh:
        pred = (pred >= clf_thresh).astype(np.float32)
    if dest_p is None:
        dest_p = h5_path[:-3] + "_pred.h5"
    if hdf5_data_key is None:
        hdf5_data_key = "raw"
    save_to_h5py([raw, pred], dest_p, [hdf5_data_key, dest_hdf5_data_key])


def overlaycubes2kzip(dest_p, vol, offset, kd_path):
    """
    Writes segmentation volume to kzip.

    Parameters
    ----------
    dest_p : str
        path to k.zip
    vol : np.array [X, Y, Z]
        Segmentation or prediction (uint)
    offset : np.array
    kd_path : str

    Returns
    -------
    np.array [Z, X, Y]
    """
    kd = KnossosDataset()
    kd.initialize_from_knossos_path(kd_path)
    kd.from_matrix_to_cubes(offset=offset, kzip_path=dest_p,
                            mags=[1], data=vol)


def xyz2zxy(vol):
    """
    Swaps axes to ELEKTRONN convention ([X, Y, Z] -> [Z, X, Y]).
    Parameters
    ----------
    vol : np.array [X, Y, Z]

    Returns
    -------
    np.array [Z, X, Y]
    """
    assert vol.ndim == 3
    # adapt data to ELEKTRONN conventions (speed-up)
    vol = vol.swapaxes(1, 0)  # y x z
    vol = vol.swapaxes(0, 2)  # z x y
    return vol


def zxy2xyz(vol):
    """
    Swaps axes to ELEKTRONN convention ([Z, X, Y] -> [X, Y, Z]).
    Parameters
    ----------
    vol : np.array [Z, X, Y]

    Returns
    -------
    np.array [X, Y, Z]
    """
    assert vol.ndim == 3
    vol = vol.swapaxes(1, 0)  # x z y
    vol = vol.swapaxes(1, 2)  # x y z
    return vol


def create_h5_from_kzip(zip_fname, kd_p, foreground_ids=None):
    """
    Create .h5 files for ELEKTRONN input. Only supports binary labels
     (0=background, 1=foreground).

    Parameters
    ----------
    zip_fname: str
    kd_p : str
    foreground_ids : iterable
        ids which have to be converted to foreground, i.e. 1. Everything
        else is considered background (0). If None, everything except 0 is
        treated as foreground.
    """
    raw, label = load_gt_from_kzip(zip_fname, kd_p)
    fname, ext = os.path.splitext(zip_fname)
    if fname[-2:] == ".k":
        fname = fname[:-2]
    create_h5_gt_file(fname, raw, label, foreground_ids)


def create_h5_gt_file(fname, raw, label, foreground_ids=None):
    """
    Create .h5 files for ELEKTRONN input from two arrays.
    Only supports binary labels (0=background, 1=foreground). E.g. for creating
    true negative cubes set foreground_ids=[] to be an empty list. If set to
    None, everything except 0 is treated as foreground.

    Parameters
    ----------
    fname: str
        Path where h5 file should be saved
    raw : np.array
    label : np.array
    foreground_ids : iterable
        ids which have to be converted to foreground, i.e. 1. Everything
        else is considered background (0). If None, everything except 0 is
        treated as foreground.
    """
    print(os.path.split(fname)[1])
    label = binarize_labels(label, foreground_ids)
    label = xyz2zxy(label)
    raw = xyz2zxy(raw)
    print("Raw:", raw.shape, raw.dtype, raw.min(), raw.max())
    print("Label:", label.shape, label.dtype, label.min(), label.max())
    print("-----------------\nGT Summary:\n%s\n" %str(Counter(label.flatten()).items()))
    if not fname[-2:] == "h5":
        fname = fname + ".h5"
    save_to_h5py([raw, label], fname, hdf5_names=["raw", "label"])


def binarize_labels(labels, foreground_ids):
    """
    Transforms label array to binary label array (0=background, 1=foreground),
    given foreground ids.

    Parameters
    ----------
    labels : np.array
    foreground_ids : iterable

    Returns
    -------
    np.array
    """
    new_labels = np.zeros_like(labels)
    if foreground_ids is None:
        if len(np.unique(labels)) > 2:
            print("------------ WARNING -------------\n"
                  "Found more than two different labels during label "
                  "conversion\n"
                  "----------------------------------")
        new_labels[labels != 0] = 1
    else:
        try:
            _ = iter(foreground_ids)
        except TypeError:
            foreground_ids = [foreground_ids]
        for ix in foreground_ids:
            new_labels[labels == ix] = 1
    labels = new_labels
    assert len(np.unique(labels)) <= 2
    assert 0 <= np.max(labels) <= 1
    assert 0 <= np.min(labels) <= 1
    return labels


def parse_movement_area_from_zip(zip_fname):
    """
    Parse MovementArea (e.g. bounding box of labeled volume) from annotation.xml
    in (k.)zip file.

    Parameters
    ----------
    zip_fname : str

    Returns
    -------
    np.array (2, 3)
        Movement Area
    """
    anno_str = read_txt_from_zip(zip_fname, "annotation.xml")
    line = re.findall("MovementArea (.*)/>", anno_str)
    assert len(line) == 1
    line = line[0]
    bb_min = np.array([re.findall('min.\w="(\d+)"', line)], dtype=np.uint)
    bb_max = np.array([re.findall('max.\w="(\d+)"', line)], dtype=np.uint)
    return np.concatenate([bb_min, bb_max])


def pred_dataset(*args, **kwargs):
    warnings.warn("'pred_dataset' will be replaced by 'predict_dataset' in"
                  " the near future.")
    return pred_dataset(*args, **kwargs)



def predict_dataset(kd_p, kd_pred_p, cd_p, model_p, imposed_patch_size=None,
                 mfp_active=False, gpu_ids=(0, ), overwrite=True):
    """
    Runs prediction on the complete knossos dataset.
    Imposed patch size has to be given in Z, X, Y!

    Parameters
    ----------
    kd_p : str
        path to knossos dataset .conf file
    kd_pred_p : str
        path to the knossos dataset head folder which will contain the prediction (will be created)
    cd_p : str
        destination folder for the chunk dataset containing prediction (will be created)
    model_p : str
        path to the ELEKTRONN2 model
    imposed_patch_size : tuple or None
        patch size (Z, X, Y) of the model
    mfp_active : bool
        activate max-fragment pooling (might be necessary to change patch_size)
    gpu_ids : tuple of int
    |   the GPU/GPUs to be used
    overwrite : bool
    |   True: fresh predictions ; False: earlier prediction continues


    Returns
    -------

    """
    if isinstance(gpu_ids, int) or len(gpu_ids) == 1:
        _pred_dataset(kd_p, kd_pred_p, cd_p, model_p, imposed_patch_size,
                 mfp_active, gpu_ids, overwrite)
    else:
        print("Starting multi-gpu prediction with GPUs:", gpu_ids)

        _multi_gpu_ds_pred(kd_p, kd_pred_p, cd_p, model_p,imposed_patch_size, gpu_ids)



def _pred_dataset(kd_p, kd_pred_p, cd_p, model_p, imposed_patch_size=None,
                 mfp_active=False, gpu_id=0,overwrite=False, i=None, n=None):
    """
    Helper function for dataset prediction. Runs prediction on whole or partial knossos dataset.
    Imposed patch size has to be given in Z, X, Y!

    Parameters
    ----------
    kd_p : str
        path to knossos dataset .conf file
    kd_pred_p : str
        path to the knossos dataset head folder which will contain the prediction
    cd_p : str
        destination folder for chunk dataset containing prediction
    model_p : str
        path tho ELEKTRONN2 model
    imposed_patch_size : tuple or None
        patch size (Z, X, Y) of the model
    mfp_active : bool
        activate max-fragment pooling (might be necessary to change patch_size)
    gpu_ix : int
    |   the GPU used
    overwrite : bool
    |   True: fresh predictions ; False: earlier prediction continues
        

    Returns
    -------

    """

    initgpu(gpu_id)
    from elektronn2.neuromancer.model import modelload
    kd = KnossosDataset()
    kd.initialize_from_knossos_path(kd_p, fixed_mag=1)

    m = modelload(model_p, imposed_patch_size=list(imposed_patch_size)
    if isinstance(imposed_patch_size, tuple) else imposed_patch_size,
                  override_mfp_to_active=mfp_active, imposed_batch_size=1)
    original_do_rates = m.dropout_rates
    m.dropout_rates = ([0.0, ] * len(original_do_rates))
    offset = m.target_node.shape.offsets
    offset = np.array([offset[1], offset[2], offset[0]], dtype=np.int)
    cd = ChunkDataset()
    cd.initialize(kd, kd.boundary, [512, 512, 256], cd_p, overlap=offset, box_coords=np.zeros(3), fit_box_size=True)

    ch_dc = cd.chunk_dict
    print('Total number of chunks for GPU/GPUs:' , len(ch_dc.keys()))

    if i is not None and n is not None:
        chunks = ch_dc.values()[i::n]
    else:
        chunks = ch_dc.values()
    print("Starting prediction of %d chunks in gpu %d\n" % (len(chunks), gpu_id))

    if not overwrite:
        for chunk in chunks:
            try:
                _ = chunk.load_chunk("pred")[0]
            except Exception as e:
                chunk_pred(chunk, m)
    else:
        for chunk in chunks:
            try:
                chunk_pred(chunk, m)
            except KeyboardInterrupt as e:
                print("Exiting out from chunk prediction: ", str(e))
                return
    save_dataset(cd)

    # single gpu processing also exports the cset to kd
    if n is None:
        kd_pred = KnossosDataset()
        kd_pred.initialize_without_conf(kd_pred_p, kd.boundary, kd.scale,
                                        kd.experiment_name, mags=[1,2,4,8])
        cd.export_cset_to_kd(kd_pred, "pred", ["pred"], [4, 4], as_raw=True,
                             stride=[256, 256, 256])


def to_knossos_dataset(kd_p, kd_pred_p, cd_p, model_p, imposed_patch_size,mfp_active=False):
    from elektronn2.neuromancer.model import modelload

    kd = KnossosDataset()
    kd.initialize_from_knossos_path(kd_p, fixed_mag=1)
    kd_pred = KnossosDataset()
    m = modelload(model_p, imposed_patch_size=list(imposed_patch_size)
    if isinstance(imposed_patch_size, tuple) else imposed_patch_size,
                  override_mfp_to_active=mfp_active, imposed_batch_size=1)
    original_do_rates = m.dropout_rates
    m.dropout_rates = ([0.0, ] * len(original_do_rates))
    offset = m.target_node.shape.offsets
    offset = np.array([offset[1], offset[2], offset[0]], dtype=np.int)
    cd = ChunkDataset()
    cd.initialize(kd, kd.boundary, [512, 512, 256], cd_p, overlap=offset, box_coords=np.zeros(3), fit_box_size=True)
    kd_pred.initialize_without_conf(kd_pred_p, kd.boundary, kd.scale,
                                    kd.experiment_name, mags=[1,2,4,8])
    cd.export_cset_to_kd(kd_pred, "pred", ["pred"], [4, 4], as_raw=True,
                         stride=[256, 256, 256])


def prediction_helper(raw, model, override_mfp=True,
                      imposed_patch_size=None):
    """
    Helper function for predicting raw volumes (range: 0 to 255; uint8).
    Will change X, Y, Z to ELEKTRONN format (Z, X, Y) and returns prediction
    in standard format [X, Y, Z]. Imposed patch size has to be given in Z, X, Y!

    Parameters
    ----------
    raw : np.array
        volume [X, Y, Z]
    model : str or model object
        path to model (.mdl)
    override_mfp : bool
    imposed_patch_size : tuple
        in Z, X, Y FORMAT!

    Returns
    -------
    np.array
        prediction data [X, Y, Z]
    """
    if type(model) == str:
        from elektronn2.neuromancer.model import modelload
        m = modelload(model, imposed_patch_size=list(imposed_patch_size)
        if isinstance(imposed_patch_size, tuple) else imposed_patch_size,
                      override_mfp_to_active=override_mfp, imposed_batch_size=1)
        original_do_rates = m.dropout_rates
        m.dropout_rates = ([0.0, ] * len(original_do_rates))
    else:
        m = model
    raw = xyz2zxy(raw)
    if raw.dtype.kind in ('u', 'i'):
        # convert to float 32 and scale it
        raw = raw.astype(np.float32) / 255.
    if not raw.dtype == np.float32:
        # assume already normalized between 0 and 1
        raw = raw.astype(np.float32)
    assert 0 <= np.max(raw) <= 1.0 and 0 <= np.min(raw) <= 1.0
    pred = m.predict_dense(raw[None,], pad_raw=True)[1]
    return zxy2xyz(pred)


def chunk_pred(ch, model, debug=False):
    """
    Helper function to write chunks.

    Parameters
    ----------
    ch : Chunk
    model : str or model object
    """
    raw = ch.raw_data()
    pred = prediction_helper(raw, model) * 255
    pred = pred.astype(np.uint8)
    ch.save_chunk(pred, "pred", "pred", overwrite=True)
    if debug:
        ch.save_chunk(raw, "pred", "raw", overwrite=False)


class NeuralNetworkInterface(object):
    """
    Experimental and almost deprecated interface class
    """
    def __init__(self, model_path, arch='marvin', imposed_batch_size=1,
                 channels_to_load=(0, 1, 2, 3), normal=False, nb_labels=2):
        self.imposed_batch_size = imposed_batch_size
        self.channels_to_load = channels_to_load
        self.arch = arch
        self._path = model_path
        self._fname = os.path.split(model_path)[1]
        self.nb_labels = nb_labels
        self.normal = normal
        if e2config.device is None:
            from elektronn2.utils.gpu import initgpu
            initgpu(0)
        from elektronn2.neuromancer.model import modelload
        self.model = modelload(model_path, replace_bn='const',
                               imposed_batch_size=imposed_batch_size)
        self.original_do_rates = self.model.dropout_rates
        self.model.dropout_rates = ([0.0, ] * len(self.original_do_rates))

    def predict_proba(self, x, verbose=False):
        x = x.astype(np.float32)
        bs = self.imposed_batch_size
        if self.arch == "rec_view":
            batches = [np.arange(i * bs, (i + 1) * bs) for i in
                       range(x.shape[1] / bs)]
            proba = np.ones((x.shape[1], 4, self.nb_labels))
        elif self.arch == "triplet":
            batches = [np.arange(i * bs, (i + 1) * bs) for i in
                       range(len(x) / bs)]
            # nb_labels represents latent space dim.; 3 -> view triplet
            proba = np.ones((len(x), self.nb_labels, 3))
        else:
            batches = [np.arange(i * bs, (i + 1) * bs) for i in
                       range(len(x) / bs)]
            proba = np.ones((len(x), self.nb_labels))
        if verbose:
            cnt = 0
            start = time.time()
            pbar = tqdm.tqdm(total=len(batches), ncols=80, leave=False,
                             unit='it', unit_scale=True, dynamic_ncols=False)
        for b in batches:
            if verbose:
                sys.stdout.write("\r%0.2f" % (float(cnt) / len(batches)))
                sys.stdout.flush()
                cnt += 1
                pbar.update()
            x_b = x[b]
            proba[b] = self.model.predict(x_b)[None, ]
        overhead = len(x) % bs
        # TODO: add proper axis handling, maybe introduce axistags
        if overhead != 0:
            new_x_b = x[-overhead:]
            if len(new_x_b) < bs:
                add_shape = list(new_x_b.shape)
                add_shape[0] = bs - len(new_x_b)
                new_x_b = np.concatenate((np.zeros((add_shape), dtype=np.float32), new_x_b))
            proba[-overhead:] = self.model.predict(new_x_b)[-overhead:]
        if verbose:
            end = time.time()
            sys.stdout.write("\r%0.2f\n" % 1.0)
            sys.stdout.flush()
            print "Prediction of %d samples took %0.2fs; %0.4fs/sample." %\
                  (len(x), end-start, (end-start)/len(x))
            pbar.close()
        return proba


def get_axoness_model_V2():
    """
    Retrained with GP dendrites. May 2018.
    """
    m = NeuralNetworkInterface("/wholebrain/u/pschuber/CNN_Training/SyConn/axon_views/g1_v2/g1_v2-FINAL.mdl",
                                  imposed_batch_size=200,
                                  nb_labels=3)
    _ = m.predict_proba(np.zeros((1, 4, 2, 128, 256)))
    return m


def get_axoness_model():
    m = NeuralNetworkInterface("/wholebrain/scratch/pschuber/CNN_Training/nupa_cnn/axoness/g5_axoness_v0_all_run2/g5_axoness_v0_all_run2-FINAL.mdl",
                                  imposed_batch_size=200,
                                  nb_labels=3)
    _ = m.predict_proba(np.zeros((1, 4, 2, 128, 256)))
    return m


def get_glia_model():
    m = NeuralNetworkInterface("/wholebrain/scratch/pschuber/NeuroPatch/neurodock/g3_gliaviews_v5_novalidset-FINAL.mdl",
                                  imposed_batch_size=300,
                                  nb_labels=2)
    _ = m.predict_proba(np.zeros((1, 1, 2, 128, 256)))
    return m


def get_tripletnet_model():
    m = NeuralNetworkInterface("/wholebrain/scratch/pschuber/CNN_Training/nupa_cnn/t_net/ssv6_tripletnet_v9/ssv6_tripletnet_v9-FINAL.mdl",
                                  imposed_batch_size=12,
                                  nb_labels=25, arch="triplet")
    _ = m.predict_proba(np.zeros((1, 4, 3, 128, 256)))
    return m


def get_tripletnet_model_ortho():
    # final model diverged...
    m = NeuralNetworkInterface("/wholebrain/u/pschuber/CNN_Training/SyConn/triplet_net_SSV/wholecell_orthoviews_v4/Backup/wholecell_orthoviews_v4-180k.mdl",
                                  imposed_batch_size=6,
                                  nb_labels=10, arch="triplet")
    _ = m.predict_proba(np.zeros((1, 4, 3, 512, 512)))
    return m


def get_celltype_model():
    m = NeuralNetworkInterface("/wholebrain/scratch/pschuber/CNN_Training/nupa_cnn/celltypes/g1_20views_v3/g1_20views_v3-FINAL.mdl",
                               imposed_batch_size=5, nb_labels=4)
    _ = m.predict_proba(np.zeros((5, 4, 20, 128, 256)))
    return m

def _multi_gpu_ds_pred(kd_p,kd_pred_p,cd_p,model_p,imposed_patch_size=None, gpu_ids=(0, 1)):

    import threading

    def start_partial_pred(kd_p, kd_pred_p, cd_p, model_p, imposed_patch_size, gpuid, i, n):

        fpath = os.path.dirname(os.path.abspath(__file__))
        path, file = os.path.split(os.path.dirname(fpath))
        cmd = "python {0}/syconn/handler/partial_ds_pred.py {1} {2} {3} {4} {5} {6} {7} {8}".format(path,kd_p,kd_pred_p,cd_p,model_p,imposed_patch_size, gpuid, i, n)
        os.system(cmd)

    for ii, gi in enumerate(gpu_ids):
        t = threading.Thread(target=start_partial_pred, args=(kd_p,kd_pred_p,cd_p,model_p,imposed_patch_size,gi,ii, len(gpu_ids)))
        t.daemon = True
        t.start()
