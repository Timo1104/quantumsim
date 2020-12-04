import warnings
from copy import copy

import numpy as np
from .state import State


class StateNumpy(State):
    """An implementation of the :class:`quantumsim.states.State` using CPU for
    computations and numpy for implementation.

    It is not focused on the performance, mainly used as a reference implementation.
    However, for small circuits it outperforms GPU implementations.
    """
    def __init__(self, qubits, pv=None, bases=None, *, dim=2, force=False):
        super().__init__(qubits, pv, bases, dim=dim, force=force)
        if pv is not None:
            if self.dim_pauli != pv.shape:
                raise ValueError(
                    '`bases` Pauli dimensionality should be the same as the '
                    'shape of `data` array.\n'
                    ' - bases shapes: {}\n - data shape: {}'
                    .format(self.dim_pauli, pv.shape))
            if pv.dtype not in (np.float16, np.float32, np.float64):
                raise ValueError(
                    '`pv` must have floating point data type, got {}'
                    .format(pv.dtype)
                )

        if isinstance(pv, np.ndarray):
            self._data = pv
        elif pv is None:
            self._data = np.array(1., dtype=float).reshape(self.dim_pauli)
        else:
            raise ValueError(
                "`pv` should be a numpy array or None, got type `{}`"
                .format(type(pv)))

    def to_pv(self):
        return self._data.copy()

    def apply_ptm(self, ptm, *qubits):
        super().apply_ptm(ptm, *qubits)
        num_qubits = len(self.qubits)
        qubit_indices = [self.qubits.index(q) for q in qubits]
        dm_in_idx = list(range(num_qubits))
        ptm_in_idx = list(qubit_indices)
        ptm_out_idx = list(range(num_qubits, num_qubits + len(qubit_indices)))
        dm_out_idx = list(dm_in_idx)
        for i_in, i_out in zip(ptm_in_idx, ptm_out_idx):
            dm_out_idx[i_in] = i_out
        self._data = np.einsum(
            self._data, dm_in_idx, ptm, ptm_out_idx + ptm_in_idx, dm_out_idx,
            optimize='greedy')

    def diagonal(self, *, get_data=True):
        no_trace_tensors = [basis.computational_basis_vectors for basis in self.bases]

        trace_argument = []
        num_qubits = len(self.qubits)
        for i, ntt in enumerate(no_trace_tensors):
            trace_argument.append(ntt)
            trace_argument.append([i + num_qubits, i])

        indices = list(range(num_qubits))
        out_indices = list(range(num_qubits, 2 * num_qubits))
        complex_dm_dimension = self.dim_hilbert ** num_qubits
        return np.einsum(self._data, indices, *trace_argument, out_indices,
                         optimize='greedy').real.reshape(complex_dm_dimension)

    def trace(self):
        # TODO: can be made more effective
        return np.sum(self.diagonal())

    def partial_trace(self, *qubits):
        super().partial_trace(*qubits)
        num_qubits = len(self.qubits)
        qubit_indices = [self.qubits.index(q) for q in qubits]
        einsum_args = [self._data, list(range(num_qubits))]
        for i, b in enumerate(self.bases):
            if i not in qubit_indices:
                einsum_args.append(b.vectors)
                einsum_args.append([i, num_qubits+i, num_qubits+i])
        einsum_args.append(qubit_indices)
        traced_dm = np.einsum(*einsum_args, optimize='greedy').real
        return self.__class__([self.bases[q] for q in qubit_indices], traced_dm)

    def meas_prob(self, qubit):
        super().meas_prob(qubit)
        num_qubits = len(self.qubits)
        einsum_args = [self._data, list(range(num_qubits))]
        for i, b in enumerate(self.bases):
            einsum_args.append(b.vectors)
            einsum_args.append([i, num_qubits+i, num_qubits+i])
        einsum_args.append([num_qubits + qubit])
        return np.einsum(*einsum_args, optimize='greedy').real

    def renormalize(self):
        tr = self.trace()
        if tr > 1e-8:
            self._data *= self.trace() ** -1
        else:
            warnings.warn(
                "Density matrix trace is 0; likely your further computation "
                "will fail. Have you projected DM on a state with zero weight?")
        return tr

    def copy(self):
        return self.__class__(copy(self.qubits), self._data.copy(), copy(self.bases),
                              force=True)
