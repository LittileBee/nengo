"""Caching capabilities for a faster build process."""

import errno
import hashlib
import inspect
import logging
import os
import struct

import numpy as np

from nengo.rc import rc
from nengo.utils.cache import bytes2human, human2bytes
from nengo.utils.compat import is_string, pickle, PY2

logger = logging.getLogger(__name__)


def safe_stat(path):
    """Does os.stat, but fails gracefully if file not found."""
    try:
        return os.stat(path)
    except OSError as err:
        if err.errno != errno.ENOENT:
            raise
    return None


def safe_remove(path):
    """Does os.remove, but fails gracefully if file not found."""
    try:
        os.remove(path)
    except OSError as err:
        if err.errno != errno.ENOENT:
            raise


class Fingerprint(object):
    """Fingerprint of an object instance.

    A finger print is equal for two instances if and only if they are of the
    same type and have the same attributes.

    The fingerprint will be used as identification for caching.

    Parameters
    ----------
    obj : object
        Object to fingerprint.
    """

    __slots__ = ['fingerprint']

    def __init__(self, obj):
        self.fingerprint = hashlib.sha1()
        try:
            self.fingerprint.update(pickle.dumps(obj, pickle.HIGHEST_PROTOCOL))
        except (pickle.PicklingError, TypeError) as err:
            raise ValueError("Cannot create fingerprint: {msg}".format(
                msg=str(err)))

    def __str__(self):
        return self.fingerprint.hexdigest()


class DecoderCache(object):
    """Cache for decoders.

    Hashes the arguments to the decoder solver and stores the result in a file
    which will be reused in later calls with the same arguments.

    Be aware that decoders should not use any global state, but only values
    passed and attributes of the object instance. Otherwise the wrong solver
    results might get loaded from the cache.

    Parameters
    ----------
    read_only : bool
        Indicates that already existing items in the cache will be used, but no
        new items will be written to the disk in case of a cache miss.
    cache_dir : str or None
        Path to the directory in which the cache will be stored. It will be
        created if it does not exists. Will use the value returned by
        :func:`get_default_dir`, if `None`.
    """

    _DECODER_EXT = '.npy'
    _SOLVER_INFO_EXT = '.pkl'

    def __init__(self, read_only=False, cache_dir=None):
        self.read_only = read_only
        if cache_dir is None:
            cache_dir = self.get_default_dir()
        self.cache_dir = cache_dir
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)

    def get_files(self):
        """Returns all of the files in the cache.

        Returns
        -------
        list of (str, int) tuples
        """
        is_cache_file = lambda f: (f.endswith(self._DECODER_EXT) or
                                   f.endswith(self._SOLVER_INFO_EXT))
        return [f for f in os.listdir(self.cache_dir) if is_cache_file(f)]

    def get_size_in_bytes(self):
        """Returns the size of the cache in bytes as an int.

        Returns
        -------
        int
        """
        stats = (safe_stat(os.path.join(self.cache_dir, f))
                 for f in self.get_files())
        return sum(st.st_size for st in stats if st is not None)

    def get_size(self, in_bytes=False):
        """Returns the size of the cache with units as a string.

        Returns
        -------
        str
        """
        return bytes2human(self.get_size_in_bytes())

    def shrink(self, limit=None):
        """Reduces the size of the cache to meet a limit.

        Parameters
        ----------
        limit : int, optional
            Maximum size of the cache in bytes.
        """
        if limit is None:
            limit = rc.get('decoder_cache', 'size')
        if is_string(limit):
            limit = human2bytes(limit)

        fileinfo = []
        for filename in self.get_files():
            path = os.path.join(self.cache_dir, filename)
            stat = safe_stat(path)
            if stat is not None:
                fileinfo.append((stat.st_atime, stat.st_size, path))

        # Remove the least recently accessed first
        fileinfo.sort()

        excess = self.get_size_in_bytes() - limit
        for _, size, path in fileinfo:
            if excess <= 0:
                break

            excess -= size
            safe_remove(path)

        # We may have removed a decoder file but not solver_info file
        # or vice versa, so we'll remove all orphans
        self.remove_orphans()

    def remove_orphans(self):
        """Removes decoders that have no solver_info, and vice versa."""
        for filename in self.get_files():
            key, ext = os.path.splitext(filename)
            decoder = self._get_decoder_path(key)
            solver_info = self._get_solver_info_path(key)
            if ext == self._DECODER_EXT and not os.path.exists(solver_info):
                safe_remove(decoder)
            elif ext == self._SOLVER_INFO_EXT and not os.path.exists(decoder):
                safe_remove(solver_info)

    def invalidate(self):
        """Invalidates the cache (i.e. removes all cache files)."""
        for filename in self.get_files():
            safe_remove(os.path.join(self.cache_dir, filename))

    @staticmethod
    def get_default_dir():
        """Returns the default location of the cache.

        Returns
        -------
        str
        """
        return rc.get('decoder_cache', 'path')

    def wrap_solver(self, solver):
        """Takes a decoder solver and wraps it to use caching.

        Parameters
        ----------
        solver : func
            Decoder solver to wrap for caching.

        Returns
        -------
        func
            Wrapped decoder solver.
        """
        def cached_solver(activities, targets, rng=None, E=None):
            try:
                args, _, _, defaults = inspect.getargspec(solver)
            except TypeError:
                args, _, _, defaults = inspect.getargspec(solver.__call__)
            args = args[-len(defaults):]
            if rng is None and 'rng' in args:
                rng = defaults[args.index('rng')]
            if E is None and 'E' in args:
                E = defaults[args.index('E')]

            key = self._get_cache_key(solver, activities, targets, rng, E)
            decoder_path = self._get_decoder_path(key)
            solver_info_path = self._get_solver_info_path(key)
            try:
                decoders = np.load(decoder_path)
                with open(solver_info_path, 'rb') as f:
                    solver_info = pickle.load(f)
            except:
                logger.info("Cache miss [{0}].".format(key))
                decoders, solver_info = solver(
                    activities, targets, rng=rng, E=E)
                if not self.read_only:
                    np.save(decoder_path, decoders)
                    with open(solver_info_path, 'wb') as f:
                        pickle.dump(solver_info, f)
            else:
                logger.info(
                    "Cache hit [{0}]: Loaded stored decoders.".format(key))
            return decoders, solver_info
        return cached_solver

    def _get_cache_key(self, solver, activities, targets, rng, E):
        h = hashlib.sha1()

        if PY2:
            h.update(str(Fingerprint(solver)))
        else:
            h.update(str(Fingerprint(solver)).encode('utf-8'))

        h.update(np.ascontiguousarray(activities).data)
        h.update(np.ascontiguousarray(targets).data)

        # rng format doc:
        # noqa <http://docs.scipy.org/doc/numpy/reference/generated/numpy.random.RandomState.get_state.html#numpy.random.RandomState.get_state>
        state = rng.get_state()
        h.update(state[0].encode())  # string 'MT19937'
        h.update(state[1].data)  # 1-D array of 624 unsigned integer keys
        h.update(struct.pack('q', state[2]))  # integer pos
        h.update(struct.pack('q', state[3]))  # integer has_gauss
        h.update(struct.pack('d', state[4]))  # float cached_gaussian

        if E is not None:
            h.update(np.ascontiguousarray(E).data)
        return h.hexdigest()

    def _get_decoder_path(self, key):
        return os.path.join(self.cache_dir, key + self._DECODER_EXT)

    def _get_solver_info_path(self, key):
        return os.path.join(self.cache_dir, key + self._SOLVER_INFO_EXT)


class NoDecoderCache(object):
    """Provides the same interface as :class:`DecoderCache` without caching."""

    def wrap_solver(self, solver):
        return solver

    def get_size_in_bytes(self):
        return 0

    def get_size(self):
        return '0 B'

    def shrink(self, limit=0):
        pass

    def invalidate(self):
        pass


def get_default_decoder_cache():
    if rc.getboolean('decoder_cache', 'enabled'):
        decoder_cache = DecoderCache(
            rc.getboolean('decoder_cache', 'readonly'))
    else:
        decoder_cache = NoDecoderCache()
    return decoder_cache