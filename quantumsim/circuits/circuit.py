import abc
import inspect
import re
from contextlib import contextmanager
from copy import copy
from itertools import chain

from .. import Operation

param_repeat_allowed = False

# TODO: implement scheduling


@contextmanager
def allow_param_repeat():
    global param_repeat_allowed
    param_repeat_allowed = True
    yield
    param_repeat_allowed = False


class CircuitBase(metaclass=abc.ABCMeta):
    _valid_identifier_re = re.compile('[a-zA-Z_][a-zA-Z0-9_]*')

    @abc.abstractmethod
    def __copy__(self):
        pass

    @abc.abstractmethod
    def operation(self, **kwargs):
        """Convert a gate to a raw operation."""
        pass

    @property
    @abc.abstractmethod
    def qubits(self):
        """Qubit names, associated with this circuit."""
        pass

    @property
    @abc.abstractmethod
    def params(self):
        """Return set of parameters, accepted by this circuit."""
        pass

    @abc.abstractmethod
    def set(self, **kwargs):
        """Either substitute a circuit parameter with a value, or rename it.

        Arguments to this function is a mapping of old parameter name to
        either its name, or a value. If type of a value provided is
        :class:`str`, it is interpreted as a new parameter name, else as a
        value for this parameter.
        """
        pass

    def __call__(self, **kwargs):
        """Convenience method to copy a circuit with parameters updated. See
        :func:`CircuitBase.set` for a description.
        """
        copy_ = copy(self)
        copy_.set(**kwargs)
        return copy_


class Gate(CircuitBase, metaclass=abc.ABCMeta):
    def __init__(self, qubits, operation, plot_metadata=None):
        """A gate without notion of timing.

        Parameters
        ----------
        qubits : str or list of str
            Names of the involved qubits
        operation : quantumsim.Operation or function
            Operation, that corresponds to this gate, or a function,
            that takes a certain number of arguments (gate parameters) and
            returns an operation.
        plot_metadata : None or dict
            Metadata, that describes how to represent a gate on a plot.
            TODO: link documentation, when plotting is ready.
        """
        self._qubits = (qubits,) if isinstance(qubits, str) else tuple(qubits)
        if isinstance(operation, Operation):
            self._operation_func = lambda: operation
            self._params_real = tuple()
            self._params = set()
        elif callable(operation):
            self._operation_func = operation
            argspec = inspect.getfullargspec(operation)
            if argspec.varargs is not None:
                raise ValueError(
                    "`operation` function can't accept free arguments.")
            if argspec.varkw is not None:
                raise ValueError(
                    "`operation` function can't accept free keyword arguments.")
            self._params_real = tuple(argspec.args)
            self._params = set(self._params_real)
        else:
            raise ValueError('`operation` argument must be either Operation, '
                             'or a function, that returns Operation.')
        self._params_set = {}
        self._params_subs = {}
        self.plot_metadata = plot_metadata or {}

    def operation(self, **kwargs):
        kwargs.update(self._params_set)  # set parameters take priority
        try:
            for name, real_name in self._params_subs.items():
                kwargs[real_name] = kwargs.pop(name)
            args = tuple(kwargs[name] for name in self._params_real)
        except KeyError as err:
            raise RuntimeError(
                "Can't construct an operation for gate {}, "
                "since parameter \"{}\" is not provided."
                .format(repr(self), err.args[0]))
        op = self._operation_func(*args)
        if not isinstance(op, Operation):
            raise RuntimeError(
                'Invalid operation function was provided for the gate {} '
                'during its creation: it must return quantumsim.Operation. '
                'See quantumsim.Gate documentation for more information.'
                .format(repr(self)))
        if not op.num_qubits == len(self.qubits):
            raise RuntimeError(
                'Invalid operation function was provided for the gate {} '
                'during its creation: its number of qubits does not match '
                'one of the gate. '
                'See quantumsim.Gate documentation for more information.'
                .format(repr(self)))
        return op

    @property
    def gates(self):
        return self,

    @property
    def qubits(self):
        return self._qubits

    @property
    def params(self):
        return self._params

    def _set_param(self, name, value):
        if name not in self._params:
            return
        real_name = self._params_subs.pop(name, name)
        if isinstance(value, str):
            if self._valid_identifier_re.match(value) is None:
                raise ValueError("\"{}\" is not a valid Python "
                                 "identifier.".format(value))
            self._params_subs[value] = real_name
            self._params.add(value)
        else:
            self._params_set[real_name] = value
        self._params.remove(name)

    def set(self, **kwargs):
        for item in kwargs.items():
            self._set_param(*item)

    def __call__(self, **kwargs):
        new_gate = copy(self)
        new_gate.set(**kwargs)
        return new_gate


class CircuitAddMixin(metaclass=abc.ABCMeta):
    @property
    @abc.abstractmethod
    def params(self):
        pass

    @abc.abstractmethod
    def __add__(self, other):
        global param_repeat_allowed
        if not param_repeat_allowed:
            common_params = self.params.intersection(other.params)
            if len(common_params) > 0:
                raise RuntimeError(
                    "The following free parameters are common for the circuits "
                    "being added, which blocks them from being set "
                    "separately later:\n"
                    "   {}\n"
                    "Rename these parameters in one of the circuits, or use "
                    "`quantumsim.circuits.allow_param_repeat` "
                    "context manager, if this is intended behaviour."
                    .format(", ".join(common_params)))


class TimeAgnostic(CircuitAddMixin, metaclass=abc.ABCMeta):
    @property
    @abc.abstractmethod
    def qubits(self):
        pass

    @property
    @abc.abstractmethod
    def gates(self):
        pass

    def __add__(self, other):
        """
        Merge two circuits, locating second one after first.

        Parameters
        ----------
        other : TimeAgnostic
            Another circuit

        Returns
        -------
        TimeAgnosticCircuit
            A merged circuit.
        """
        super().__add__(other)
        all_gates = self.gates + other.gates
        all_qubits = self.qubits + tuple(q for q in other.qubits
                                         if q not in self.qubits)
        return TimeAgnosticCircuit(all_qubits, all_gates)


class TimeAware(CircuitAddMixin, metaclass=abc.ABCMeta):
    @property
    @abc.abstractmethod
    def time_start(self):
        pass

    @time_start.setter
    @abc.abstractmethod
    def time_start(self, time):
        pass

    @property
    @abc.abstractmethod
    def time_end(self):
        pass

    @time_end.setter
    @abc.abstractmethod
    def time_end(self, time):
        pass

    @property
    @abc.abstractmethod
    def duration(self):
        pass

    def shift(self, *, time_start=None, time_end=None):
        """

        Parameters
        ----------
        time_start : float or None
        time_end : float or Nont

        Returns
        -------
        TimeAware
        """
        if time_start is not None and time_end is not None:
            raise ValueError('Only one argument is accepted.')
        copy_ = copy(self)
        if time_start is not None:
            copy_.time_start = time_start
        elif time_end is not None:
            copy_.time_end = time_end
        else:
            raise ValueError('Specify time_start or time_end')
        return copy_

    def _qubit_time_start(self, qubit):
        for gate in self.gates:
            if qubit in gate.qubits:
                return gate.time_start

    def _qubit_time_end(self, qubit):
        for gate in reversed(self.gates):
            if qubit in gate.qubits:
                return gate.time_end

    def __add__(self, other):
        """

        Parameters
        ----------
        other : TimeAware
            Another circuit.

        Returns
        -------
        TimeAwareCircuit
        """
        super().__add__(other)
        shared_qubits = set(self.qubits).intersection(other.qubits)
        if len(shared_qubits) > 0:
            other_shifted = other.shift(time_start=max(
                (self._qubit_time_end(q) - other._qubit_time_start(q)
                 for q in shared_qubits)) + 2*other.time_start)
        else:
            other_shifted = copy(other)
        qubits = tuple(chain(self.qubits,
                             (q for q in other.qubits if q not in self.qubits)))
        gates = tuple(chain((copy(g) for g in self.gates),
                            other_shifted.gates))
        return TimeAwareCircuit(qubits, gates)


class Circuit(CircuitBase, metaclass=abc.ABCMeta):
    def __init__(self, qubits, gates):
        self._gates = tuple(gates)
        self._qubits = tuple(qubits)
        self._params_cache = None

    @property
    def qubits(self):
        return self._qubits

    @property
    def gates(self):
        return self._gates

    @property
    def params(self):
        if self._params_cache is None:
            self._params_cache = set(chain(*(g.params for g in self._gates)))
        return self._params_cache

    def operation(self, **kwargs):
        operations = []
        for gate in self._gates:
            qubit_indices = tuple(self._qubits.index(qubit) for qubit in
                                  gate.qubits)
            operations.append(gate.operation(**kwargs).at(*qubit_indices))
        return Operation.from_sequence(operations)

    def set(self, **kwargs):
        for gate in self._gates:
            gate.set(**kwargs)
        self._params_cache = None


class TimeAgnosticGate(Gate, TimeAgnostic):
    def __copy__(self):
        copy_ = self.__class__(
            self._qubits, self._operation_func, self.plot_metadata)
        copy_._params_set = copy(self._params_set)
        copy_._params_subs = copy(self._params_subs)
        return copy_


class TimeAgnosticCircuit(Circuit, TimeAgnostic):
    def __copy__(self):
        # Need a shallow copy of all included gates
        copy_ = self.__class__(self._qubits, (copy(g) for g in self._gates))
        copy_._params_cache = self._params_cache
        return copy_


class TimeAwareGate(Gate, TimeAware):

    def __init__(self, qubits, operation, duration=0.,
                 time_start=0., plot_metadata=None):
        """TimedGate - a gate with a well-defined timing.

        Parameters:
        -----
        duration : dictionary of floats
            the duration of the gate on each of the qubits
        time_start : dictionary of floats or None
            an absolute start time on each of the qubits
        """
        super().__init__(qubits, operation, plot_metadata)
        self._duration = duration
        self._time_start = time_start

    def __copy__(self):
        copy_ = self.__class__(
            self._qubits, self._operation_func,
            self._duration, self._time_start, self.plot_metadata)
        copy_._params_set = copy(self._params_set)
        copy_._params_subs = copy(self._params_subs)
        return copy_

    @property
    def time_start(self):
        return self._time_start

    @time_start.setter
    def time_start(self, time):
        self._time_start = time

    @property
    def time_end(self):
        return self._time_start + self._duration

    @time_end.setter
    def time_end(self, time):
        self._time_start = time - self._duration

    @property
    def duration(self):
        return self._duration


class TimeAwareCircuit(Circuit, TimeAware):
    def __init__(self, qubits, gates):
        super().__init__(qubits, gates)
        self._time_start = min((g.time_start for g in self._gates))
        self._time_end = max((g.time_end for g in self._gates))

    @property
    def time_start(self):
        return self._time_start

    @time_start.setter
    def time_start(self, time):
        shift = time - self._time_start
        for g in self._gates:
            g.time_start += shift
        self._time_start += shift
        self._time_end += shift

    @property
    def time_end(self):
        return self._time_end

    @time_end.setter
    def time_end(self, time):
        shift = time - self.time_end
        self.time_start += shift

    @property
    def duration(self):
        return self._time_end - self._time_start

    def __copy__(self):
        # Need a shallow copy of all included gates
        copy_ = self.__class__(self._qubits, (copy(g) for g in self._gates))
        copy_._params_cache = self._params_cache
        return copy_
