# This file is part of quantumsim. (https://github.com/brianzi/quantumsim)
# (c) 2016 Brian Tarasinski
# Distributed under the GNU GPLv3. See LICENSE.txt or
# https://www.gnu.org/licenses/gpl.txt

from .backend import DensityMatrixBase

import numpy as np
import pycuda.driver as drv
import pycuda.gpuarray as ga

import pytools

import pycuda.autoinit
import pycuda.reduction

# load the kernels
from pycuda.compiler import SourceModule, DEFAULT_NVCC_FLAGS

import sys
import os

package_path = os.path.dirname(os.path.realpath(__file__))

mod = None

for kernel_file in [
        sys.prefix +
        "/pycudakernels/primitives.cu",
        package_path +
        "/primitives.cu"]:
    try:
        with open(kernel_file, "r") as kernel_source_file:
            mod = SourceModule(
                kernel_source_file.read(), options=DEFAULT_NVCC_FLAGS+[
                    "--default-stream", "per-thread", "-lineinfo"])
            break
    except FileNotFoundError:
        pass

if mod is None:
    raise ImportError("could not find primitives.cu")

pycuda.autoinit.context.set_shared_config(
    drv.shared_config.EIGHT_BYTE_BANK_SIZE)

_two_qubit_general_ptm = mod.get_function("two_qubit_general_ptm")
_two_qubit_general_ptm.prepare("PPPIIIII")
_multitake = mod.get_function("multitake")
_multitake.prepare("PPPPPPI")

sum_along_axis = pycuda.reduction.ReductionKernel(
        dtype_out=np.float64,
        neutral="0", reduce_expr="a+b",
        map_expr="(i/stride) % dim == offset ? in[i] : 0",
        arguments="const double *in, unsigned int stride, unsigned int dim, "
                  "unsigned int offset"
        )


class DensityMatrix(DensityMatrixBase):
    _gpuarray_cache = {}

    def __init__(self, bases, data=None):
        """Create a new density matrix for several qudits.

        Parameters
        ----------

        bases : a list of :class:`ptm.PauliBasis`
            A descrption of the basis for the subsystems.

        data : :class:`numpy.ndarray`, :class:`pycuda.gpuarray.array`,\
        :class:`pycuda.driver.DeviceAllocation`, or `None`.
            Must be of size (2**no_qubits, 2**no_qubits); is copied to GPU if
            not already there.  Only upper triangle is relevant.  If data is
            `None`, create a new density matrix with all qubits in ground
            state.
        """
        super().__init__(bases, data)

        if isinstance(data, ga.GPUArray):
            if self.shape != data.shape:
                raise ValueError(
                    "`bases` Pauli dimensionality should be the same as the "
                    "shape of `data` array.\n"
                    "bases shapes: {}, data shape: {}"
                    .format(self.shape, data.shape))
            self.data = data
        elif isinstance(data, np.ndarray):
            raise NotImplementedError('TODO: implement for Numpy arrays')
        elif data is None:
            self.data = np.zeros(self.shape, np.float64)
            ground_state_index = [pb.comp_basis_indices[0]
                                  for pb in self.bases]
            self.data[tuple(ground_state_index)] = 1
            self.data = ga.to_gpu(self.data)
        else:
            raise ValueError("Unknown type of `data`: {}".format(type(data)))

        self.data.gpudata.size = self.data.nbytes
        self._work_data = ga.empty_like(self.data)
        self._work_data.gpudata.size = self._work_data.nbytes

    def _cached_gpuarray(self, array):
        """
        Given a numpy array,
        calculate the python hash of its bytes;

        If it is not found in the cache, upload to gpu
        and store in cache, otherwise return cached allocation.
        """

        array = np.ascontiguousarray(array)
        key = hash(array.tobytes())
        try:
            array_gpu = self._gpuarray_cache[key]
        except KeyError:
            array_gpu = ga.to_gpu(array)
            self._gpuarray_cache[key] = array_gpu

        # for testing: read_back_and_check!

        return array_gpu

    def _check_cache(self):
        for k, v in self._gpuarray_cache.items():
            a = v.get().tobytes()
            assert hash(a) == k

    def trace(self):
        # todo there is a smarter way of doing this with pauli-dirac basis
        return np.sum(self.get_diag())

    def renormalize(self):
        """Renormalize to trace one."""
        tr = self.trace()
        self.data *= np.float(1 / tr)

    def copy(self):
        """Return a deep copy of this Density."""
        data_cp = self.data.copy()
        cp = self.__class__(self.bases, data=data_cp)
        return cp

    def to_array(self):
        """Return the entries of the density matrix as a dense numpy ndarray.
        """
        # dimensions = [2]*self.no_qubits
        #
        # host_dm = dm_general_np.DensityGeneralNP(
        #     dimensions, data=self.data.get()).to_array()
        #
        # return host_dm
        raise NotImplementedError()

    def get_diag(self, target_array=None, get_data=True, flatten=True):
        """Obtain the diagonal of the density matrix.

        Parameters
        ----------
        target_array : None or pycuda.gpuarray.array
            An already-allocated GPU array to which the data will be copied.
            If `None`, make a new GPU array.

        get_data : boolean
            Whether the data should be copied from the GPU.
        flatten : boolean
            TODO docstring
        """
        diag_bases = [pb.get_classical_subbasis() for pb in self.bases]
        diag_shape = [db.dim_pauli for db in diag_bases]
        diag_size = pytools.product(diag_shape)

        if target_array is None:
            if self._work_data.gpudata.size < diag_size*8:
                self._work_data.gpudata.free()
                self._work_data = ga.empty(diag_shape, np.float64)
                self._work_data.gpudata.size = self._work_data.nbytes
            target_array = self._work_data
        else:
            if target_array.size < diag_size:
                raise ValueError(
                    "Size of `target_gpu_array` is too small ({}).\n"
                    "Should be at least {}."
                    .format(target_array.size, diag_size))

        idx = [[pb.comp_basis_indices[i]
                for i in range(pb.dim_hilbert)
                if pb.comp_basis_indices[i] is not None]
               for pb in self.bases]

        idx_j = np.array(list(pytools.flatten(idx))).astype(np.uint32)
        idx_i = np.cumsum([0]+[len(i) for i in idx][:-1]).astype(np.uint32)

        xshape = np.array(self.data.shape, np.uint32)
        yshape = np.array(diag_shape, np.uint32)

        xshape_gpu = self._cached_gpuarray(xshape)
        yshape_gpu = self._cached_gpuarray(yshape)

        idx_i_gpu = self._cached_gpuarray(idx_i)
        idx_j_gpu = self._cached_gpuarray(idx_j)

        block = (2**8, 1, 1)
        grid = (max(1, (diag_size-1)//2**8 + 1), 1, 1)

        if len(yshape) == 0:
            # brain-dead case, but should be handled according to exp.
            target_array.set(self.data.get())
        else:
            _multitake.prepared_call(
                    grid,
                    block,
                    self.data.gpudata,
                    target_array.gpudata,
                    idx_i_gpu.gpudata, idx_j_gpu.gpudata,
                    xshape_gpu.gpudata, yshape_gpu.gpudata,
                    np.uint32(len(yshape))
                    )

        if get_data:
            if flatten:
                return (target_array.get()
                        .ravel()[:diag_size])
            else:
                return (target_array.get()
                        .ravel()[:diag_size]
                        .reshape(diag_shape))
        else:
            return ga.GPUArray(shape=diag_shape,
                               gpudata=target_array.gpudata,
                               dtype=np.float64)

    def apply_two_qubit_ptm(self, qubit0, qubit1, ptm, basis_out=None):
        """Apply a two-qubit Pauli transfer matrix to qubit `bit0` and `bit1`.

        Parameters
        ----------
        ptm: array-like
            A two-qubit ptm in the basis of `bit0` and `bit1`. Must be a 4D
            matrix with dimensions, that correspond to the qubits.
        qubit0 : int
            Index of first qubit
        qubit1: int
            Index of second qubit
        basis_out: tuple of two qs2.bases.PauliBasis
        """
        self._validate_qubit(qubit0, 'qubit0')
        self._validate_qubit(qubit1, 'qubit1')
        if len(ptm.shape) != 4:
            raise ValueError(
                "`ptm` must be a 4D array, got {}D".format(len(ptm.shape)))

        # bit1 must be the more significant bit (bit 0 is msb)
        if qubit1 > qubit0:
            qubit1, qubit0 = qubit0, qubit1
            ptm = np.einsum("abcd -> badc", ptm)
            if basis_out is not None:
                basis_out = list(basis_out)
                basis_out[1], basis_out[0] = basis_out[0], basis_out[1]

        new_shape = list(self.data.shape)
        dim1_out, dim0_out, dim1_in, dim0_in = ptm.shape
        assert new_shape[qubit0] == dim0_in
        assert new_shape[qubit1] == dim1_in
        new_shape[qubit0] = dim0_out
        new_shape[qubit1] = dim1_out
        new_size = pytools.product(new_shape)
        new_size_bytes = new_size * 8

        if self._work_data.gpudata.size < new_size_bytes:
            # reallocate
            self._work_data.gpudata.free()
            self._work_data = ga.empty(new_shape, np.float64)
            self._work_data.gpudata.size = self._work_data.nbytes
        else:
            # reallocation not required,
            # reshape but reuse allocation
            self._work_data = ga.GPUArray(
                    shape=new_shape,
                    dtype=np.float64,
                    gpudata=self._work_data.gpudata,
                    )

        ptm_gpu = self._cached_gpuarray(ptm)

        # dint = max(min(16, self.data.size//(dim1_out*dim0_out)), 1)

        rest_shape = new_shape.copy()
        rest_shape[qubit0] = 1
        rest_shape[qubit1] = 1

        dint = 1
        for i in sorted(rest_shape):
            if i*dint > 256//(dim1_out*dim0_out):
                break
            else:
                dint *= i

        # dim_a_out, dim_b_out, d_internal (arbitrary)
        block = (dim1_out, dim0_out, dint)
        blocksize = dim0_out*dim1_out*dint
        sh_mem_size = dint*dim0_in*dim1_in  # + ptm.size
        grid_size = max(1, (new_size-1)//blocksize+1)
        grid = (grid_size, 1, 1)

        dim_z = pytools.product(self.data.shape[qubit0 + 1:])
        dim_y = pytools.product(self.data.shape[qubit1 + 1:qubit0])
        dim_rho = new_size  # self.data.size

        _two_qubit_general_ptm.prepared_call(
            grid,
            block,
            self.data.gpudata,
            self._work_data.gpudata,
            ptm_gpu.gpudata,
            dim1_in, dim0_in,
            dim_z,
            dim_y,
            dim_rho,
            shared_size=8*sh_mem_size)

        self.data, self._work_data = self._work_data, self.data

        if basis_out is not None:
            self.bases[qubit0] = basis_out[0]
            self.bases[qubit1] = basis_out[1]

    def apply_single_qubit_ptm(self, qubit, ptm, basis_out=None):
        """Apply a one-qubit Pauli transfer matrix to qubit bit.

        Parameters
        ----------
        qubit: int
            Qubit index
        ptm: array-like
            A PTM in the basis of a qubit.
        basis_out: qs2.bases.PauliBasis or None
            If provided, will convert qubit basis to specified after the PTM
            application.
        """
        if basis_out:
            raise NotImplementedError('Seems like specifying output basis '
                                      'does not work yet.')
        new_shape = list(self.data.shape)
        self._validate_qubit(qubit, 'bit')

        # TODO Refactor to use self._validate_ptm
        if len(ptm.shape) != 2:
            raise ValueError(
                "`ptm` must be a 2D array, got {}D".format(len(ptm.shape)))

        dim_bit_out, dim_bit_in = ptm.shape
        new_shape[qubit] = dim_bit_out
        assert new_shape[qubit] == dim_bit_out
        new_size = pytools.product(new_shape)
        new_size_bytes = new_size * 8

        if self._work_data.gpudata.size < new_size_bytes:
            # reallocate
            self._work_data.gpudata.free()
            self._work_data = ga.empty(new_shape, np.float64)
            self._work_data.gpudata.size = self._work_data.nbytes
        else:
            # reallocation not required,
            # reshape but reuse allocation
            self._work_data = ga.GPUArray(
                    shape=new_shape,
                    dtype=np.float64,
                    gpudata=self._work_data.gpudata,
                    )

        ptm_gpu = self._cached_gpuarray(ptm)

        dint = min(64, self.data.size//dim_bit_in)
        block = (1, dim_bit_out, dint)
        blocksize = dim_bit_out*dint
        grid_size = max(1, (new_size-1)//blocksize+1)
        grid = (grid_size, 1, 1)

        dim_z = pytools.product(self.data.shape[qubit + 1:])
        dim_y = pytools.product(self.data.shape[:qubit])
        dim_rho = new_size  # self.data.size

        _two_qubit_general_ptm.prepared_call(
            grid,
            block,
            self.data.gpudata,
            self._work_data.gpudata,
            ptm_gpu.gpudata,
            1, dim_bit_in,
            dim_z,
            dim_y,
            dim_rho,
            shared_size=8 * (ptm.size + blocksize))

        self.data, self._work_data = self._work_data, self.data

        if basis_out is not None:
            self.bases[qubit] = basis_out

    def add_ancilla(self, basis, state):
        """Add an ancilla with `basis` and with state state."""

        # TODO: express in terms off `apply_ptm`

        # figure out the projection matrix
        ptm = np.zeros(basis.dim_pauli)
        ptm[basis.comp_basis_indices[state]] = 1

        # make sure work_data is large enough, reshape it
        # TODO hacky as fuck: we put the allocated size
        # into the allocation object by hand

        new_shape = tuple([basis.dim_pauli] + list(self.data.shape))
        new_size = pytools.product(new_shape)
        new_size_bytes = new_size * 8

        if self._work_data.gpudata.size < new_size_bytes:
            # reallocate
            self._work_data.gpudata.free()
            self._work_data = ga.empty(new_shape, np.float64)
            self._work_data.gpudata.size = self._work_data.nbytes
        else:
            # reallocation not required,
            # reshape but reuse allocation
            self._work_data = ga.GPUArray(
                    shape=new_shape,
                    dtype=np.float64,
                    gpudata=self._work_data.gpudata,
                    )

        # perform projection
        ptm_gpu = self._cached_gpuarray(ptm)

        dim_bit = basis.dim_pauli
        dint = min(64, new_size_bytes//8//dim_bit)
        block = (1, dim_bit, dint)
        blocksize = dim_bit*dint
        grid_size = max(1, (new_size_bytes // 8 - 1) // blocksize + 1)
        grid = (grid_size, 1, 1)

        dim_z = self.data.size  # 1
        dim_y = 1
        dim_rho = new_size  # self.data.size

        _two_qubit_general_ptm.prepared_call(
            grid,
            block,
            self.data.gpudata,
            self._work_data.gpudata,
            ptm_gpu.gpudata,
            1, 1,
            dim_z,
            dim_y,
            dim_rho,
            shared_size=8 * (ptm.size + blocksize)
        )

        self.data, self._work_data = self._work_data, self.data
        self.bases = [basis] + self.bases

    def partial_trace(self, qubit):
        """ Return the diagonal of the reduced density matrix of a qubit.

        Parameters
        ----------
        qubit: int
            Index of the qubit.
        """
        self._validate_qubit(qubit, 'qubit')

        # TODO on graphics card, optimize for tracing out?
        diag = self.get_diag(get_data=False)

        res = []
        stride = diag.strides[qubit]//8
        dim = diag.shape[qubit]
        for offset in range(dim):
            pt = sum_along_axis(diag, stride, dim, offset)
            res.append(pt)

        return [p.get() for p in res]

    def project(self, qubit, state, lazy_alloc=True):
        """Remove a qubit from the density matrix by projecting
        on a computational basis state.

        bit: int
            Which bit to project.
        state: int
            Which state in the Hilbert space to project on
        lazy_alloc: boolean
            If True, do not allocate a smaller space for the new matrix,
            instead leave it at the same size as now, in anticipation of a
            future increase in size.
        """
        self._validate_qubit(qubit, 'bit')

        new_shape = list(self.data.shape)
        new_shape[qubit] = 1

        new_size_bytes = self.data.nbytes//self.bases[qubit].dim_pauli

        # TODO hack: put the allocated size into the allocation by hand
        if self._work_data.gpudata.size < new_size_bytes or not lazy_alloc:
            # reallocate
            self._work_data.gpudata.free()
            self._work_data = ga.empty(new_shape, np.float64)
            self._work_data.gpudata.size = self._work_data.nbytes
        else:
            # reallocation not required,
            # reshape but reeuse allocation
            self._work_data = ga.GPUArray(
                    shape=new_shape,
                    dtype=np.float64,
                    gpudata=self._work_data.gpudata,
                    )

        idx = []
        # TODO: can be built more efficiently
        for i, pb in enumerate(self.bases):
            if i == qubit:
                idx.append([pb.comp_basis_indices[state]])
            else:
                idx.append(list(range(pb.dim_pauli)))

        idx_j = np.array(list(pytools.flatten(idx))).astype(np.uint32)
        idx_i = np.cumsum([0]+[len(i) for i in idx][:-1]).astype(np.uint32)

        xshape = np.array(self.data.shape, np.uint32)
        yshape = np.array(new_shape, np.uint32)

        xshape_gpu = self._cached_gpuarray(xshape)
        yshape_gpu = self._cached_gpuarray(yshape)

        idx_i_gpu = self._cached_gpuarray(idx_i)
        idx_j_gpu = self._cached_gpuarray(idx_j)

        block = (2**8, 1, 1)
        grid = (max(1, (self._work_data.size-1)//2**8 + 1), 1, 1)

        _multitake.prepared_call(
                grid,
                block,
                self.data.gpudata,
                self._work_data.gpudata,
                idx_i_gpu.gpudata, idx_j_gpu.gpudata,
                xshape_gpu.gpudata, yshape_gpu.gpudata,
                np.uint32(len(xshape))
                )

        self.data, self._work_data = self._work_data, self.data

        subbase_idx = [self.bases[qubit].comp_basis_indices[state]]
        self.bases[qubit] = self.bases[qubit].get_subbasis(subbase_idx)
