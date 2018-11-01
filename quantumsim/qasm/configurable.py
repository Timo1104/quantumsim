import json
import numpy as np
import re
import warnings
from collections import OrderedDict
from itertools import chain

from .. import circuit as ct
from .. import ptm


class ConfigurationError(RuntimeError):
    pass


class QASMError(RuntimeError):
    pass


# Reverse compatibility
QasmError = QASMError


class NotSupportedError(RuntimeError):
    pass


def _dict_merge_recursive(*dicts):
    if len(dicts) == 1 and not isinstance(dicts[0], dict):
        dicts = dicts[0]
    rv = dict()

    for d in dicts:
        for k, v in d.items():
            if isinstance(rv.get(k), dict) and isinstance(v, dict):
                rv[k] = _dict_merge_recursive(rv[k], v)
            else:
                rv[k] = v

    return rv


class Decomposer:
    """Instances of this class are callable objects, that take a QASM
    instruction as input and return its expansion, according to definition in
    `config['gate_decomposition']`.
    """
    _arg_matcher = re.compile(r"%(\d+)")

    def __init__(self, alias, expansion):
        s = alias.strip()
        arg_nums = self._arg_matcher.findall(s)
        # This RE should match provided alias (for example 'x q0,q1' for
        # alias 'x %0,%1' and, if match is successful, return ('q0', 'q1')
        # tuple.
        self._matcher_re = re.compile(
            r'^\s*{}\s*$'.format(self._arg_matcher.sub(r'(\\w+)', s)))
        if not len(arg_nums) == len(set(arg_nums)):
            raise ConfigurationError(
                'Alias has repeated "%n"-argument, that is not supported.')
        # For example, expansion for `'x %0,%1'` is
        # `['h %0', 'h %1', 'cn %1,%0']`. Then, self._format_strings should be
        # `['h {0}', 'h {1}, 'cn {1},{0}'] to return proper commands after
        # formatting with tuple, parsed by self._matcher_re.
        self._format_strings = []
        for instr in expansion:
            fs = str(instr)
            for i, k in enumerate(arg_nums):
                fs = re.sub(
                    r"%{id}\b".format(id=k), r'{{{}}}'.format(i), fs)
            self._format_strings.append(fs)

    def _match(self, instr):
        return self._matcher_re.match(instr) is not None

    def __call__(self, instr):
        match = self._matcher_re.match(instr)
        if match is not None:
            values = match.groups()
            return [expander.format(*values)
                    for expander in self._format_strings]
        else:
            return None


class ConfigurableParser:
    """Parser for QASM files, that uses OpenQL-compatible JSON files for
    defining QASM grammar.

    Mandatory configuration should have the following JSON structure
    (or corresponding to it data structure)::

        {
          "instructions": {
            "i q0": {
              "duration": 20,
              "qubits": ["q0"],
              "kraus_repr": [
                [
                  [1.0, 0.0], [0.0, 0.0],
                  [0.0, 0.0], [1.0, 0.0]
                ]
              ],
            }
          }
        }

    Here `"i q0"` is an actual QASM instruction, `"duration"` is the duration
    of a corresponding gate in arbitrary time units, `"qubits"` is a list of
    qubits, `"kraus_repr"` is a list of Kraus operator for a correspondent
    gate. Each Kraus operator is represented as a matrix, that has concatenated
    strings and each its number `a` is represented as `[Re(a), Im(a)]`.
    Same instructions for different qubits need to be specifies separately.

    Additionally, one can specify the following entries::

        {
          "gate_decomposition": {
            "cnot %0,%1": [
              "ry90 %0",
              "cz %0,%1",
              "ry90 %1"
            ]
          },
          "simulation_settings": {
            "error_models": {
              "q0": {
                "error_model": "t1t2",
                "t1": 30000000.0,
                "t2": 10000000.0,
                "frac1_0": 0.0001,
                "frac1_1": 0.9999
              }
            }
          }
        }

    `gate_decomposition` defines parametrized gate decomposition,
    `"simulation_settings"` defines error models for different qubits.
    Currently, only "t1t2" is supported.
    If `"simulation_settings"` is not defined in configuration, qubits are
    considered to be ideal.

    Parameters
    ----------

    cfg1, cfg2, ... : strings or dictionaries
        One or more configuration files. If string is provided, it is
        interpreted as a filename of a JSON file and dictionary is parsed out
        of it. Each next config overrides items, defined in previous config.

    gate_ptm_mapping : collections.OrderedDict of function or None
        A dictionary, that maps gate instructions, as they are specified in
        QASM file, to Pauli transfer matrices of correspondent gates.

        Keys of this dictionary are of type :class:`str`, and they are
        interpreted as Python regular expressions patterns (see :mod:`re`
        documentation), that target gates should match. For example,
        if you provide `"rx"` as a key, it will match all `"rx"` instructions
        (including `"rx180"` and `"rx90"`), and if you specify `"rx q0"` --
        only instructions on qubit `"q0"`.

        Values of the dictionary are functions, that take non-perturbed
        Kraus representation of a quantum operation, and return a PTM
        in :math:`0xy1` basis. Module :mod:`quantumsim.ptm` might have
        some useful helpers to construct these functions.

        If no mapping is provided for some gate, its Kraus is converted to
        Pauli transfer matrix, assuming no errors at all (perfect gate).
        Gates are matched in the order of items in the dictionary, first one
        has priority.

    Raises
    ------
    ConfigurationError
        If the configuration is wrong or insufficient.
    """

    # Regexp, used to identify whether a string is a valid QASM instruction
    _valid_instruction_re = re.compile(r"^[a-zA-Z\d_.]+ q\d+(?:,q\d+)?$")

    # noinspection PyTypeChecker
    def __init__(self, *args, gate_ptm_mapping=None):
        self._gate_ptm_mapping = OrderedDict()
        if gate_ptm_mapping:
            for k, v in gate_ptm_mapping.items():
                self._gate_ptm_mapping[re.compile(k)] = v
        self._gate_ptm_mapping[self._valid_instruction_re] = \
            self._gate_ptm_mapping_default

        if len(args) == 0:
            raise ConfigurationError('No config files provided')

        config_dicts = []
        for i, config in enumerate(args):
            if isinstance(config, str):
                # assuming path to JSON file
                with open(config, 'r') as c:
                    config_dicts.append(json.load(c))
            elif isinstance(config, dict):
                # Assuming dictionary
                config_dicts.append(config)
            else:
                raise ConfigurationError(
                    'Could not cast config entry number {} to dictionary'
                    .format(i))
        configuration = _dict_merge_recursive(*config_dicts)

        try:
            self._instructions = configuration['instructions']
        except KeyError:
            raise ConfigurationError(
                'Could not find "instructions" block in config')

        decompositions = configuration.get('gate_decomposition', {})
        self._decomposers = {Decomposer(al, dec)
                             for al, dec in decompositions.items()}

        self._simulation_settings = configuration.get('simulation_settings',
                                                      None)
        self._parse_func_table = {
            'asap': self._parse_circuit_asap,
            'alap': self._parse_circuit_alap,
        }

    def parse(self, qasm, rng=None, *, ordering='ALAP',
              time_start=None, time_end=None, toposort=True):
        """Parses QASM to the list of circuits.

        Parameters
        ----------
        qasm : str or iterable
            Filename or iterator over strings
        rng : numpy.random.RandomState, int or None
            Random number generator or seed to initialize a new one.
            If None is specified, calculations will not be reproducible.
        ordering: str
            How to order gates in time in circuit.
            Currently supported options are `'ALAP'` (as late as possible,
            default) or `'ASAP'` (as soon as possible).
        time_start: float or None
            Beginning time for the circuits.
            Mutually exclusive with `time_end`.
            If both are None, defaults to 0.
        time_end: float or None
            Ending time for the circuits.
            Mutually exclusive with `time_start`.
            If both are None, defaults to 0.
        toposort: bool
            Whether to apply topological ordering while circuit creation
            (see :func:`quantumsim.tp.partial_greedy_toposort`). Defaults to
            True.
        """
        return list(self.gen_circuits(
            qasm, rng, ordering=ordering,
            time_start=time_start, time_end=time_end, toposort=toposort))

    def gen_circuits(self, qasm, rng=None, *, ordering='ALAP',
                     time_start=None, time_end=None, toposort=True):
        """Returns a generator over the circuits, defined in QASM.
        Circuits are constructed lazily.

        Parameters
        ----------
        qasm : str or iterable
            Filename or iterator over strings
        rng : numpy.random.RandomState, int or None
            Random number generator or seed to initialize a new one.
            If None is specified, calculations will not be reproducible.
        ordering: str
            How to order gates in time in circuit.
            Currently supported options are `'ALAP'` (as late as possible,
            default) or `'ASAP'` (as soon as possible).
        time_start: float or None
            Beginning time for the circuits.
            Mutually exclusive with `time_end`.
            If both are None, defaults to 0.
        time_end: float or None
            Ending time for the circuits.
            Mutually exclusive with `time_start`.
            If both are None, defaults to 0.
        toposort: bool
            Whether to apply topological ordering while circuit creation
            (see :func:`quantumsim.tp.partial_greedy_toposort`). Defaults to
            True.
        """
        rng = ct._ensure_rng(rng)
        if isinstance(qasm, str):
            return self._gen_circuits_fn(qasm, rng, ordering,
                                         time_start, time_end, toposort)
        else:
            return self._gen_circuits_fp(qasm, rng, ordering,
                                         time_start, time_end, toposort)

    @staticmethod
    def _gen_circuits_src(fp):
        """Returns a generator over the circuits, that generates
        `(title, [op1, op2, ...])`
        (circuit title and circuit operations, QASM strings)

        Parameters
        ----------
        fp: iterable
            Generator of strings
        """
        circuit_title = None
        circuit_src = []
        for line_full in fp:
            line = line_full.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('.'):
                if circuit_title:
                    yield circuit_title, circuit_src
                circuit_title = line[1:]
                circuit_src = []
            else:
                circuit_src.append(line)
        if circuit_title:
            yield circuit_title, circuit_src
        else:
            warnings.warn("Could not find any circuits in the QASM file.")

    def _gen_circuits_fp(self, fp, rng, ordering, time_start, time_end,
                         toposort):
        """Returns a generator over the circuits, provided iterator or
        generator of strings `fp`.
        """
        # Getting the initial statement with the number of qubits
        rng = ct._ensure_rng(rng)
        qubits_re = re.compile(r'^\s*qubits\s+(\d+)')
        n_qubits = None

        for line in fp:
            m = qubits_re.match(line)
            if m:
                n_qubits = int(m.groups()[0])
                break

        if not n_qubits:
            raise QASMError('Number of qubits is not specified')

        for title, source in self._gen_circuits_src(fp):
            # We pass the same rng to avoid correlations between measurements,
            # everything else must be re-initialized
            circuit = self._parse_circuit(source, ordering, rng,
                                          time_start, time_end, toposort)
            circuit.title = title
            yield circuit

    def _gen_circuits_fn(self, fn, rng, ordering, time_start, time_end,
                         toposort):
        """Returns a generator over the circuits, provided QASM filename `fn`.
        """
        with open(fn, 'r') as fp:
            generator = self._gen_circuits_fp(fp, rng, ordering,
                                              time_start, time_end, toposort)
            for circuit in generator:
                yield circuit

    def _add_qubit(self, circuit, qubit_name):
        """Adds qubit `qubit_name` to `circuit`."""
        if self._simulation_settings:
            try:
                params = self._simulation_settings['error_models'][qubit_name]
            except KeyError:
                raise ConfigurationError(
                    'Could not find simulation settings for qubit {}'
                    .format(qubit_name))
            em = params.get('error_model', None)
            if em == 't1t2':
                circuit.add_qubit(qubit_name,
                                  t1=params.get('t1', np.inf),
                                  t2=params.get('t2', np.inf))
            else:
                raise ConfigurationError(
                    'Unknown error model for qubit "{}": "{}"'
                    .format(qubit_name, em))
        else:
            # No simulation settings provided -- assuming ideal qubits
            circuit.add_qubit(qubit_name)

    # noinspection PyUnusedLocal
    def _instruction_to_gate(self, instruction, rng):
        """Returns a tuple of gate with its starting time set to 0 and gate's
        duration"""
        # TODO After fixing https://gitlab.com/quantumsim/quantumsim/issues/7
        # this should be refactored, since duration will be bundled into the
        # gate object itself.
        gate_spec = self._instructions[instruction]
        qubits = gate_spec['qubits']

        kraus_to_ptm_func = None
        for matcher, func in self._gate_ptm_mapping.items():
            if matcher.match(instruction):
                kraus_to_ptm_func = func
                break
        if not kraus_to_ptm_func:
            raise QASMError('Invalid instruction: "{}"'.format(instruction))

        if self._gate_is_ignored(gate_spec):
            return None, 0.
        elif self._gate_is_measurement(gate_spec):
            # FIXME This is not precise: currently we are interested in
            # actual density matrix instead of measurement result in the end,
            # so we insert ButterflyGate instead of the measurement. Ideally
            # this piece of code should be redone for the general case.
            if self._simulation_settings:
                qubit_name = qubits[0]
                try:
                    params = \
                        self._simulation_settings['error_models'][qubit_name]
                except KeyError:
                    raise ConfigurationError(
                        'Could not find simulation settings for qubit {}'
                        .format(qubit_name))
                p_exc = params['frac1_0']
                p_dec = 1 - params['frac1_1']
                gate = ct.ButterflyGate(qubits[0], 0.,
                                        p_exc=p_exc, p_dec=p_dec)
                gate.label = self._gate_label_by_instruction(instruction)
            else:
                gate = None
            duration = 0.
        else:
            n_qubits = len(gate_spec['qubits'])
            ptm_ = kraus_to_ptm_func(gate_spec['kraus_repr'])
            self._validate_ptm(instruction, ptm_, n_qubits)
            label = self._gate_label_by_instruction(instruction)
            duration = gate_spec['duration']
            gate = ct.SinglePTMGate(qubits[0], 0., ptm_) if n_qubits == 1 \
                else ct.TwoPTMGate(qubits[0], qubits[1], ptm_, 0.)
            gate.label = self._gate_label_by_instruction(instruction)
            duration = gate_spec['duration']

        return gate, duration

    @staticmethod
    def _validate_ptm(name, ptm_, n_qubits):
        if ptm_.shape == (4, 4):
            n_qubits_ptm = 1
        elif ptm_.shape == (16, 16):
            n_qubits_ptm = 2
        else:
            raise ConfigurationError(
                'Invalid shape of a Pauli transfer matrix for gate "{}": '
                'must be 4x4 matrix for single-qubit gate or 16x16 matrix '
                'for two-qubit gate, got shape {}'.format(name, ptm_.shape))

        if n_qubits != n_qubits_ptm:
            raise ConfigurationError(
                'Invalid gate specification for gate "{}": number of '
                'involved qubits according to specification is {}, '
                'number of involved qubits according to Pauli transfer '
                'matrix is {}.'.format(name, n_qubits, n_qubits_ptm))

    @staticmethod
    def _gate_label_by_instruction(instruction):
        """Returns instruction command."""
        return instruction.strip().split(" ")[0]

    def _format_circuit(self, qubits, gates, toposort):
        circuit = ct.Circuit()
        for qubit in qubits:
            self._add_qubit(circuit, qubit)
        for gate in gates:
            circuit.add_gate(gate)

        # tmin might be important, tmax is defined by the last gate --
        # idling gates afterwards are useless
        # circuit.add_waiting_gates(tmin=tmin, tmax=None)
        circuit.add_waiting_gates(tmin=0, tmax=None)
        circuit.order(toposort=toposort)
        return circuit

    def _parse_circuit(self, source, ordering, rng,
                       time_start, time_end, toposort):
        """Parses circuit, defined by set of instructions `source`,
        to a Quantumsim circuit.
        """
        source_decomposed = list(self._decompose_instructions(source))
        # Here we get all qubits, that actually participate in circuit
        try:
            parse_func = self._parse_func_table[ordering.lower()]
        except KeyError:
            raise RuntimeError('Unknown ordering: {}'.format(ordering))
        return parse_func(instructions=source_decomposed, rng=rng,
                          time_start=time_start, time_end=time_end,
                          toposort=toposort)

    @staticmethod
    def _gate_is_ignored(gate_spec):
        out = gate_spec['type'] == 'none'
        return out

    @staticmethod
    def _gate_is_measurement(gate_spec):
        out = gate_spec['type'] == 'readout'
        if out:
            if len(gate_spec['qubits']) != 1:
                raise NotSupportedError(
                    'Only single-qubit measurements are supported')
        return out

    def _parse_circuit_alap(self, instructions, rng, time_start, time_end,
                            toposort):
        """Gets list of gate specifications (as parsed from configuration) and
        returns list of gates constructed, scheduling each gate as late
        as possible.
        """
        rng = ct._ensure_rng(rng)
        instructions = list(instructions)
        qubits = set(chain(*(self._instructions[line]['qubits']
                             for line in instructions)))
        current_times = {qubit: 0. for qubit in qubits}
        gates = []
        for instruction in reversed(instructions):
            gate, duration = self._instruction_to_gate(instruction, rng)
            # gate is validated already inside `_instruction_to_gate`
            if gate is None:
                continue
            gate_time_end = min((current_times[qubit]
                                 for qubit in gate.involved_qubits))
            gate_time_start = gate_time_end - duration
            gate.set_time(0.5 * (gate_time_start + gate_time_end))
            for qubit in gate.involved_qubits:
                current_times[qubit] = gate_time_start
            gates.append(gate)

        gates = list(reversed(gates))
        time_min = min(current_times.values())
        if time_start is not None and time_end is not None:
            raise RuntimeError('Only start or end time of the circuit '
                               'can be specified')
        elif time_end is not None:
            time_shift = time_end
        else:
            if time_start is None:
                time_start = 0.
            time_shift = time_start - time_min

        if not np.allclose(time_shift, 0.):
            for gate in gates:
                gate.increment_time(time_shift)

        return self._format_circuit(qubits, gates, toposort)

    def _parse_circuit_asap(self, instructions, rng, time_start, time_end,
                            toposort):
        """Gets list of gate specifications (as parsed from configuration) and
        returns list of gates constructed, scheduling each gate as soon
        as possible.
        """
        rng = ct._ensure_rng(rng)
        instructions = list(instructions)
        qubits = set(chain(*(self._instructions[line]['qubits']
                             for line in instructions)))
        current_times = {qubit: 0. for qubit in qubits}
        gates = []
        for instruction in instructions:
            gate, duration = self._instruction_to_gate(instruction, rng)
            # gate is validated already inside `_instruction_to_gate`
            if gate is None:
                continue
            gate_time_start = max((current_times[qubit]
                                   for qubit in gate.involved_qubits))
            gate_time_end = gate_time_start + duration
            gate.set_time(0.5 * (gate_time_start + gate_time_end))
            for qubit in gate.involved_qubits:
                current_times[qubit] = gate_time_end
            gates.append(gate)

        time_max = max(current_times.values())
        if time_start is not None and time_end is not None:
            raise RuntimeError('Only start or end time of the circuit '
                               'can be specified')
        elif time_end is None:
            time_shift = time_start or 0.
        else:
            time_shift = time_end - time_max

        if not np.allclose(time_shift, 0.):
            for gate in gates:
                gate.increment_time(time_shift)
        return self._format_circuit(qubits, gates, toposort)

    def _decompose_instructions(self, source):
        """Returns a generator of instructions from `source` (can be also a
        generator), expanding all aliases, specified in configuration in
        "gate_decompositions" field, so that we have only instructions from
        "instructions" field of configuration. Raises `QasmError`, if this is
        impossible.
        """
        for s in source:
            # FIXME: Here we filter out prepz gates, based on name. Generally
            # this should be done, based on gate_spec, in the method
            # _gate_is_ignored, but it does not get any signature of it yet.
            if s.startswith('prepz'):
                continue
            elif s in self._instructions.keys():
                yield s
                continue
            # trying to decompose instruction
            maybe_decomposed = self._try_decompose(s)
            if maybe_decomposed is not None:
                # These may be also aliases, so we call here the same
                # function recursively.
                # FIXME Would be nice to insert here some protection
                # against infinite recursion.
                for s1 in self._decompose_instructions(maybe_decomposed):
                    yield s1
                continue
            raise QASMError("Unknown QASM instruction: {}".format(s))

    def _try_decompose(self, instr):
        """If instruction matches alias, this method returns expansion of
        instruction with this alias. If it does not match, it returns `None`.
        """
        for decomposer in self._decomposers:
            result = decomposer(instr)
            if result is not None:
                return result
        return None

    @staticmethod
    def _gate_ptm_mapping_default(kraus_repr):
        n_items = len(kraus_repr[0])
        for i, matrix in enumerate(kraus_repr[1:]):
            if len(matrix) != n_items:
                raise ConfigurationError(
                    "Got inconsistent Kraus representation: matrix number 1 "
                    "has {} elements, matrix number {} has {} elements."
                    .format(n_items, i, len(matrix))
                )
        kr_spec = np.array(kraus_repr, dtype=float)
        if n_items == 4:
            # Single-qubit gate
            kr_list = [(m[:, 0] + m[:, 1] * 1j).reshape((2, 2)) for m in
                       kr_spec]
            ptm_list = [ptm.single_kraus_to_ptm(kr) for kr in kr_list]
            return np.sum(ptm_list, axis=0)
        elif n_items == 16:
            kr_list = [(m[:, 0] + m[:, 1] * 1j).reshape((4, 4)) for m in
                       kr_spec]
            ptm_list = [ptm.double_kraus_to_ptm(kr) for kr in kr_list]
            return np.sum(ptm_list, axis=0)
        else:
            raise ConfigurationError(
                "Kraus representation should have either 4 items in a "
                "matrix for single-qubit gate or 16 items for two qubit-gate. "
                "Got {} items instead.".format(n_items)
            )
