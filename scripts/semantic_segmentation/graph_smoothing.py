# SyConn
# Copyright (c) 2018
# All rights reserved

import numpy as np
from knossos_utils.skeleton_utils import load_skeleton
from sklearn.neighbors import KDTree
from syconn.proc.meshes import MeshObject, rgb2id_array, id2rgb_array_contiguous
from syconn.handler.basics import majority_element_1d
from syconn.proc.rendering import render_sso_coords, _render_mesh_coords,\
    render_sso_coords_index_views
from syconn.reps.super_segmentation import SuperSegmentationObject
import networkx as nx
from syconn.proc.graphs import split_subcc
from itertools import combinations


def graph_creator(indices, vertices):
    G = nx.Graph()
    if (indices.ndim == 1) or (indices.ndim == 2 and indices.shape[1] == 1):
        triangles = indices.reshape((-1,3))
    else:
        triangles = indices
    if (vertices.ndim == 1) or (vertices.ndim == 2 and vertices.shape[1] == 1):
        vertices = vertices.reshape((-1,3))
    for i in range(len(triangles)):
        v0, v1, v2 = vertices[triangles[i][0]], vertices[triangles[i][1]], vertices[triangles[i][2]]
        w01 = np.linalg.norm(v0-v1)
        w12 = np.linalg.norm(v1-v2)
        w02 = np.linalg.norm(v0-v2)
        G.add_edge(triangles[i][0], triangles[i][1], weight=w01)
        G.add_edge(triangles[i][1], triangles[i][2], weight=w12)
        G.add_edge(triangles[i][0], triangles[i][2], weight=w02)
    return G


def bfs_smoothing(indices, vertices, vertex_labels, n_nodes=30):
    """
    Creates a BFS smoothing on the mesh surface for every vertex. Takes into
    account 'n_nodes' closest to every vertex to perform majority vote.

    Parameters
    ----------
    indices : np.array
    vertices : np.array
    vertex_labels :
    n_nodes :

    Returns
    -------

    """
    G = graph_creator(indices, vertices)
    nn_dc = split_subcc(G, max_nb=n_nodes)
    maj_vertex_labels = np.zeros_like(vertex_labels)
    for node_ix, subgraph in nn_dc.iteritems():
        nn_ixs = subgraph.nodes()
        nn_vertex_labels = vertex_labels[nn_ixs]
        labels, cnts = np.unique(nn_vertex_labels, return_counts=True)
        maj_vote = labels[np.argmax(cnts)]
        maj_vertex_labels[node_ix] = maj_vote
    return maj_vertex_labels


def new_label_views():
    return

def generate_label_views(kzip_path, gt_type="spgt"):
    assert gt_type in ["axgt", "spgt"], "Currently only spine and axon GT is supported"
    palette = generate_palette(3) # currently in all GT types we only need 3 foreground labels
    sso_id = int(re.findall("/(\d+).", kzip_path)[0])
    sso = SuperSegmentationObject(sso_id, version=gt_type)
    indices, vertices, normals = sso.mesh

    # # Load mesh
    vertices = vertices.reshape((-1, 3))

    # load skeleton
    skel = load_skeleton(kzip_path)["skeleton"]
    skel_nodes = list(skel.getNodes())

    node_coords = np.array([n.getCoordinate() * sso.scaling for n in skel_nodes])
    node_labels = np.array([str2intconverter(n.getComment(), gt_type) for n in skel_nodes], dtype=np.int)
    node_coords = node_coords[node_labels != -1]
    node_labels = node_labels[node_labels != -1]

    # create KD tree from skeleton node coordinates
    tree = KDTree(node_coords)

    # transfer labels from skeleton to mesh
    dist, ind = tree.query(vertices, k=1)

    vertex_labels = node_labels[ind]  # retrieving labels of vertices

    # if no skeleton nodes closer than 2um were found set their label
    # to 2 (shaft; basically this is our background class)
    vertex_labels[dist > 2000] = 2
    # smooth vertex labels
    tree = KDTree(vertices)
    _, ind = tree.query(vertices, k=50)
    # now extract k-closest labels for every vertex
    vertex_labels = vertex_labels[ind]
    # apply majority voting; remove auxiliary axis
    vertex_labels = np.apply_along_axis(majority_element_1d, 1, vertex_labels)[:, 0]
    color_array = palette[vertex_labels].astype(np.float32)/255
    # Initializing mesh object with ground truth coloring
    mo = MeshObject("neuron", indices, vertices, color=color_array)

    # use downsampled locations for view locations, only if they are close to a
    # labeled skeleton node
    locs = np.concatenate(sso.sample_locations())
    dist, ind = tree.query(locs)
    locs = locs[dist[:, 0] < 2000]
    print("Rendering label views.")

    # DEBUG PART
    loc_text = ''
    for i, c in enumerate(locs):
        loc_text += str(i+1) + "\t" + str((c / np.array([10, 10, 20])).astype(np.int)) +'\n' #rescalling to the voxel grid
    with open("/u/shum/view_coordinates_files/view_coords_{}.txt".format(sso_id), "w") as f:
        f.write(loc_text)
    # DEBUG PART
    label_views, rot_mat = _render_mesh_coords(locs, mo, depth_map=False,
                                      return_rot_matrices=True, smooth_shade=False)
    # sso._pred2mesh(node_coords, node_labels, dest_path="/wholebrain/u/shum/sso_%d_skeletonlabels.k.zip" %
    #                                                    sso.id, ply_fname="0.ply")
    print("Rendering index views.")
    index_views = render_sso_coords_index_views(sso, locs,
                                                rot_matrices=rot_mat)
    print("Rendering raw views.")
    raw_views = render_sso_coords(sso, locs)
    print("Remapping views.")
    return raw_views, remap_rgb_labelviews(label_views, palette)[:, None], rgb2id_array(index_views)[:, None]