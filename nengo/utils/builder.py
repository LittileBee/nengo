"""These are helper functions that various backends may find useful for
generating their own Builder system.
"""

from __future__ import absolute_import
import collections

import numpy as np

import nengo


def full_transform(conn, slice_pre=True, slice_post=True, allow_scalars=True):
    """Compute the full transform for a connection.

    Parameters
    ----------
    conn : Connection
        The connection for which to compute the full transform.
    slice_pre : boolean, optional (True)
        Whether to compute the pre slice as part of the transform.
    slice_post : boolean, optional (True)
        Whether to compute the post slice as part of the transform.
    allow_scalars : boolean, optional (True)
        If true (default), will not make scalars into full transforms when
        not using slicing, since these work fine in the reference builder.
        If false, these scalars will be turned into scaled identity matrices.
    """
    transform = conn.transform
    pre_slice = conn.pre_slice if slice_pre else slice(None)
    post_slice = conn.post_slice if slice_post else slice(None)

    if pre_slice == slice(None) and post_slice == slice(None):
        if transform.ndim == 2:
            # transform is already full, so return a copy
            return np.array(transform)
        elif transform.size == 1 and allow_scalars:
            return np.array(transform)

    # Create the new transform matching the pre/post dimensions
    func_size = conn.function_info.size
    size_in = (conn.pre_obj.size_out if func_size is None
               else func_size) if slice_pre else conn.size_mid
    size_out = conn.post_obj.size_in if slice_post else conn.size_out
    new_transform = np.zeros((size_out, size_in))

    if transform.ndim < 2:
        new_transform[np.arange(size_out)[post_slice],
                      np.arange(size_in)[pre_slice]] = transform
        return new_transform
    elif transform.ndim == 2:
        repeated_inds = lambda x: (
            not isinstance(x, slice) and np.unique(x).size != len(x))
        if repeated_inds(pre_slice):
            raise ValueError("Input object selection has repeated indices")
        if repeated_inds(post_slice):
            raise ValueError("Output object selection has repeated indices")

        rows_transform = np.array(new_transform[post_slice])
        rows_transform[:, pre_slice] = transform
        new_transform[post_slice] = rows_transform
        # Note: the above is a little obscure, but we do it so that lists of
        #  indices can specify selections of rows and columns, rather than
        #  just individual items
        return new_transform
    else:
        raise ValueError("Transforms with > 2 dims not supported")


def default_n_eval_points(n_neurons, dimensions):
    """A heuristic to determine an appropriate number of evaluation points.

    This is used by builders to generate a sufficiently large sample
    from a vector space in order to solve for accurate decoders.

    Parameters
    ----------
    n_neurons : int
        The number of neurons in the ensemble that will be sampled.
        For a connection, this would be the number of neurons in the
        `pre` ensemble.
    dimensions : int
        The number of dimensions in the ensemble that will be sampled.
        For a connection, this would be the number of dimensions in the
        `pre` ensemble.
    """
    return max(np.clip(500 * dimensions, 750, 2500), 2 * n_neurons)


def objs_and_connections(network):
    """Given a Network, returns all (ensembles + nodes, connections)."""
    objs = list(network.ensembles + network.nodes)
    connections = list(network.connections)
    for subnetwork in network.networks:
        subobjs, subconnections = objs_and_connections(subnetwork)
        objs.extend(subobjs)
        connections.extend(subconnections)
    return objs, connections


def generate_graphviz(objs, connections):
    """Create a .gv file with this set of objects and connections

    Parameters
    ----------
    objs : list of Nodes and Ensembles
        All the objects in the model
    connections : list of Connections
        All the Connections in the model

    Returns the text contents of the desired .dot file

    This can be useful for debugging and testing Builders that manipulate
    the model graph before construction.
    """
    text = []
    text.append('digraph G {')
    for obj in objs:
        text.append('  "%d" [label="%s"];' % (id(obj), obj.label))

    def label(transform):
        # determine the label for a connection based on its transform
        transform = np.asarray(transform)
        if len(transform.shape) == 0:
            return ''
        return '%dx%d' % transform.shape

    for c in connections:
        text.append('  "%d" -> "%d" [label="%s"];' % (
            id(c.pre_obj), id(c.post_obj), label(c.transform)))
    text.append('}')
    return '\n'.join(text)


def _create_replacement_connection(c_in, c_out):
    """Generate a new Connection to replace two through a passthrough Node"""
    assert c_in.post_obj is c_out.pre_obj
    assert c_in.post_obj.output is None

    # determine the filter for the new Connection
    if c_in.synapse is None:
        synapse = c_out.synapse
    elif c_out.synapse is None:
        synapse = c_in.synapse
    else:
        raise NotImplementedError('Cannot merge two filters')
        # Note: the algorithm below is in the right ballpark,
        #  but isn't exactly the same as two low-pass filters
        # filter = c_out.filter + c_in.filter

    function = c_in.function
    if c_out.function is not None:
        raise Exception('Cannot remove a Node with a '
                        'function being computed on it')

    # compute the combined transform
    transform = np.dot(full_transform(c_out), full_transform(c_in))

    # check if the transform is 0 (this happens a lot
    #  with things like identity transforms)
    if np.all(transform == 0):
        return None

    c = nengo.Connection(c_in.pre_obj, c_out.post_obj,
                         synapse=synapse,
                         transform=transform,
                         function=function,
                         add_to_container=False)
    return c


def remove_passthrough_nodes(objs, connections,  # noqa: C901
        create_connection_fn=_create_replacement_connection):
    """Returns a version of the model without passthrough Nodes

    For some backends (such as SpiNNaker), it is useful to remove Nodes that
    have 'None' as their output.  These nodes simply sum their inputs and
    use that as their output. These nodes are defined purely for organizational
    purposes and should not affect the behaviour of the model.  For example,
    the 'input' and 'output' Nodes in an EnsembleArray, which are just meant to
    aggregate data.

    Note that removing passthrough nodes can simplify a model and may be useful
    for other backends as well.  For example, an EnsembleArray connected to
    another EnsembleArray with an identity matrix as the transform
    should collapse down to D Connections between the corresponding Ensembles
    inside the EnsembleArrays.

    Parameters
    ----------
    objs : list of Nodes and Ensembles
        All the objects in the model
    connections : list of Connections
        All the Connections in the model

    Returns the objs and connections of the resulting model.  The passthrough
    Nodes will be removed, and the Connections that interact with those Nodes
    will be replaced with equivalent Connections that don't interact with those
    Nodes.
    """

    inputs, outputs = find_all_io(connections)
    result_conn = list(connections)
    result_objs = list(objs)

    # look for passthrough Nodes to remove
    for obj in objs:
        if isinstance(obj, nengo.Node) and obj.output is None:
            result_objs.remove(obj)

            # get rid of the connections to and from this Node
            for c in inputs[obj]:
                result_conn.remove(c)
                outputs[c.pre_obj].remove(c)
            for c in outputs[obj]:
                result_conn.remove(c)
                inputs[c.post_obj].remove(c)

            # replace those connections with equivalent ones
            for c_in in inputs[obj]:
                if c_in.pre_obj is obj:
                    raise Exception('Cannot remove a Node with feedback')

                for c_out in outputs[obj]:
                    c = create_connection_fn(c_in, c_out)
                    if c is not None:
                        result_conn.append(c)
                        # put this in the list, since it might be used
                        # another time through the loop
                        outputs[c.pre_obj].append(c)
                        inputs[c.post_obj].append(c)

    return result_objs, result_conn


def split_ensemblearrays(network):
    for net in network.networks:
        split_ensemblearrays(net)

        if isinstance(net, nengo.networks.EnsembleArray):
            split_ensemblearray(net, network)


def split_ensemblearray(array, parent):
    # create_connection_fn = _create_replacement_connection
    if not isinstance(array, nengo.networks.EnsembleArray):
        raise ValueError("'array' must be an EnsembleArray")
    if not isinstance(parent, nengo.Network) or array not in parent.networks:
        raise ValueError("'parent' must be parent network")

    inputs, outputs = find_all_io(parent.connections + array.connections)

    n_splits = 2
    preserve_zeros = True
    n_ensembles = array.n_ensembles
    D = array.dimensions_per_ensemble

    # assert no extra connections
    if array.n_ensembles != len(array.ea_ensembles):
        raise ValueError("Number of ensembles does not match")

    ea_ensemble_set = set(array.ea_ensembles)
    if len(outputs[array.input]) != n_ensembles or (
            set(c.post for c in outputs[array.input]) != ea_ensemble_set):
        raise ValueError("Extra connections from array input")

    for output in (getattr(array, name) for name in array.output_sizes):
        if len(inputs[output]) != n_ensembles or (
                set(c.pre for c in inputs[output]) != ea_ensemble_set):
            raise ValueError("Extra connections to array output")

    # equally distribute ensembles between partitions
    sizes = np.zeros(n_splits, dtype=int)
    j = 0
    for i in range(n_ensembles):
        sizes[j] += 1
        j = (j + 1) % len(sizes)

    indices = np.zeros(len(sizes) + 1, dtype=int)
    indices[1:] = np.cumsum(sizes)

    # make new input nodes
    with array:
        new_inputs = [nengo.Node(size_in=size * D,
                                 label="%s%d" % (array.input.label, i))
                      for i, size in enumerate(sizes)]

    # remove connections from old input node
    for conn in array.connections[:]:
        if conn.pre_obj is array.input and conn.post in array.ea_ensembles:
            array.connections.remove(conn)

    # make connections from new input nodes to ensembles
    for i, inp in enumerate(new_inputs):
        i0, i1 = indices[i], indices[i+1]
        for j, ens in enumerate(array.ea_ensembles[i0:i1]):
            with array:
                nengo.Connection(inp[j*D:(j+1)*D], ens, synapse=None)

    # if input was probed, remove the probe and create new ones
    for probe in parent.probes[:]:
        if probe.target is array.input:
            parent.probes.remove(probe)

            with parent:
                for i, inp in enumerate(new_inputs):
                    nengo.Probe(
                        inp, label="%s%d" % (probe.label, i),
                        synapse=probe.synapse)

    # make connections into EnsembleArray
    for c_in in inputs[array.input]:

        # remove connection to old node
        parent.connections.remove(c_in)

        pre_outputs = outputs[c_in.pre_obj]
        pre_outputs.remove(c_in)

        transform = full_transform(c_in, False, True, False)

        # make connections to new nodes
        for i, inp in enumerate(new_inputs):
            i0, i1 = indices[i], indices[i+1]
            sub_transform = transform[i0*D:i1*D, :]

            if preserve_zeros or np.any(sub_transform):
                with parent:
                    new_conn = nengo.Connection(
                        c_in.pre, inp,
                        synapse=c_in.synapse,
                        function=c_in.function,
                        transform=sub_transform)

                inputs[inp].append(new_conn)
                pre_outputs.append(new_conn)

    # remove old input node
    array.nodes.remove(array.input)
    array.input = None

    # loop over outputs
    for output_name in array.output_sizes:
        old_output = getattr(array, output_name)
        output_sizes = array.output_sizes[output_name]

        # make new output nodes
        new_outputs = []
        for i in range(n_splits):
            i0, i1 = indices[i], indices[i+1]
            i_sizes = output_sizes[i0:i1]
            with array:
                new_output = nengo.Node(size_in=sum(i_sizes),
                                        label="%s%d" % (old_output.label, i))
            new_outputs.append(new_output)

            i_inds = np.zeros(len(i_sizes) + 1, dtype=int)
            i_inds[1:] = np.cumsum(i_sizes)

            # connect ensembles to new output node
            for j, e in enumerate(array.ea_ensembles[i0:i1]):
                old_conns = [c for c in array.connections
                             if c.pre is e and c.post_obj is old_output]
                assert len(old_conns) == 1
                old_conn = old_conns[0]

                # remove old connection from ensemble
                array.connections.remove(old_conn)

                # add new connection from ensemble
                j0, j1 = i_inds[j], i_inds[j+1]
                with array:
                    nengo.Connection(
                        e, new_output[j0:j1],
                        synapse=old_conn.synapse,
                        function=old_conn.function,
                        transform=old_conn.transform)

        # if output was probed, remove the probe and create new ones
        for probe in parent.probes[:]:
            if probe.target is old_output:
                parent.probes.remove(probe)

                with parent:
                    for j, out in enumerate(new_outputs):
                        nengo.Probe(
                            out, label="%s%d" % (probe.label, j),
                            synapse=probe.synapse)

        # connect new outputs to external model
        output_sizes = [n.size_out for n in new_outputs]
        output_inds = np.zeros(len(output_sizes) + 1, dtype=int)
        output_inds[1:] = np.cumsum(output_sizes)

        for c_out in outputs[old_output]:
            assert c_out.function is None

            # remove connection to old node
            parent.connections.remove(c_out)

            post_inputs = inputs[c_out.post_obj]
            post_inputs.remove(c_out)

            transform = full_transform(c_out, True, True, False)

            # add connections to new nodes
            for i, out in enumerate(new_outputs):
                i0, i1 = output_inds[i], output_inds[i+1]
                sub_transform = transform[:, i0:i1]

                if preserve_zeros or np.any(sub_transform):
                    with parent:
                        new_conn = nengo.Connection(
                            out, c_out.post,
                            synapse=c_out.synapse,
                            transform=sub_transform)

                    outputs[out].append(new_conn)
                    post_inputs.append(new_conn)

        # remove old output node
        array.nodes.remove(old_output)
        setattr(array, output_name, None)


def find_all_io(connections):
    """Build up a list of all inputs and outputs for each object"""
    inputs = collections.defaultdict(list)
    outputs = collections.defaultdict(list)
    for c in connections:
        inputs[c.post_obj].append(c)
        outputs[c.pre_obj].append(c)
    return inputs, outputs
