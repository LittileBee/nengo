import numpy as np

import nengo
from nengo.dists import Choice
from nengo.networks.ensemblearray import EnsembleArray
from nengo.utils.stdlib import nested


def Product(n_neurons, dimensions, input_magnitude=1, config=None, net=None):
    """Computes the element-wise product of two equally sized vectors."""
    if net is None:
        net = nengo.Network(label="Product")

    if config is None:
        config = nengo.Config(nengo.Ensemble)

    with nested(net, config):
        net.A = nengo.Node(size_in=dimensions, label="A")
        net.B = nengo.Node(size_in=dimensions, label="B")
        net.output = nengo.Node(size_in=dimensions, label="output")

        net.sq1 = EnsembleArray(
            n_neurons // 2, n_ensembles=dimensions, ens_dimensions=1,
            radius=input_magnitude * np.sqrt(2))
        net.sq2 = EnsembleArray(
            n_neurons // 2, n_ensembles=dimensions, ens_dimensions=1,
            radius=input_magnitude * np.sqrt(2))
        nengo.Connection(
            net.A, net.sq1.input, transform=1. / np.sqrt(2.), synapse=None)
        nengo.Connection(
            net.B, net.sq1.input, transform=1. / np.sqrt(2.), synapse=None)
        nengo.Connection(
            net.A, net.sq2.input, transform=1. / np.sqrt(2.), synapse=None)
        nengo.Connection(
            net.B, net.sq2.input, transform=-1. / np.sqrt(2.), synapse=None)

        nengo.Connection(
            net.sq1.add_output('square', np.square), net.output, transform=.5)
        nengo.Connection(
            net.sq2.add_output('square', np.square), net.output, transform=-.5)

    return net


def dot_product_transform(dimensions, scale=1.0):
    """Returns a transform for output to compute the scaled dot product."""
    return scale * np.ones((1, dimensions))
