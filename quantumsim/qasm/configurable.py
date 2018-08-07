import json
import numpy as np
import re
import warnings
from itertools import chain

from .. import circuit as ct
from .. import ptm


class ConfigurationError(RuntimeError):
    pass


class QasmError(RuntimeError):
    pass


class NotSupportedError(RuntimeError):
    pass


class Decomposer:
    """Instances of this class are callable objects, that take a QASM
    instruction as input and return its expansion, according to definition in
    `config['gate_decomposition']`.
    """
    _arg_matcher = re.compile(r"\%(\d+)")

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
                    r"\%{id}\b".format(id=k), r'{{{}}}'.format(i), fs)
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

    def __init__(self, *args):
        if len(args) == 0:
            raise ConfigurationError('No config files provided')

        configuration = {}
        for i, config in enumerate(args):
            if isinstance(config, str):
                with open(config, 'r') as c:
                    cfg = json.load(c)
            else:
                # Assuming dictionary
                cfg = config
            try:
                configuration.update(cfg)
            except TypeError:
                raise ConfigurationError(
                    'Could not cast config entry number {} to dictioary'
                    .format(i))

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
        self._gates_order_table = {
            'asap': self._gates_order_asap,
            'alap': self._gates_order_alap,
        }

    def parse(self, qasm, rng=None, *, ordering='ALAP',
              time_start=None, time_end=None):
        return list(self.gen_circuits(
            qasm, rng, ordering=ordering,
            time_start=time_start, time_end=time_end))

    def gen_circuits(self, qasm, rng=None, *, ordering='ALAP',
                     time_start=None, time_end=None):
        rng = ct._ensure_rng(rng)
        if isinstance(qasm, str):
            return self._gen_circuits_fn(qasm, rng, ordering,
                                         time_start, time_end)
        else:
            return self._gen_circuits(qasm, rng, ordering,
                                      time_start, time_end)

    @staticmethod
    def _gen_circuits_src(fp):
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

    def _gen_circuits(self, fp, rng, ordering, time_start, time_end):
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
            raise QasmError('Number of qubits is not specified')

        for title, source in self._gen_circuits_src(fp):
            # We pass the same rng to avoid correlations between measurements,
            # everything else must be re-initialized
            yield self._parse_circuit(title, source, ordering, rng,
                                      time_start, time_end)

    def _gen_circuits_fn(self, filename, rng, ordering, time_start, time_end):
        with open(filename, 'r') as fp:
            generator = self._gen_circuits(fp, rng, ordering,
                                           time_start, time_end)
            for circuit in generator:
                yield circuit

    def _add_qubit(self, circuit, qubit_name):
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

    def _gate_spec_to_gate(self, gate_spec, gate_label, rng):
        """Returns gate with time set to 0. and its duration"""
        duration = gate_spec['duration']
        qubits = gate_spec['qubits']
        if self._gate_is_ignored(gate_spec):
            return None, 0.
        elif self._gate_is_measurement(gate_spec):
            # FIXME VO: To comply with Brian's code, I insert here
            # ButterflyGate. I suppose this is not as this should be, need to
            # consult and get this correctly.
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
                gate.label = gate_label
            else:
                gate = None
        elif self._gate_is_single_qubit(gate_spec):
            m = np.array(gate_spec['matrix'], dtype=float)
            # TODO: verify if it is not conjugate
            kraus = (m[:, 0] + m[:, 1]*1j).reshape((2, 2))
            gate = ct.SinglePTMGate(
                qubits[0],
                0.,
                ptm.single_kraus_to_ptm(kraus)
            )
            gate.label = gate_label
        elif self._gate_is_two_qubit(gate_spec):
            m = np.array(gate_spec['matrix'], dtype=float)
            # TODO: verify if it is not conjugate
            kraus = (m[:, 0] + m[:, 1]*1j).reshape((4, 4))
            gate = ct.TwoPTMGate(
                qubits[0],
                qubits[1],
                ptm.double_kraus_to_ptm(kraus),
                0.,
            )
            gate.label = gate_label
        else:
            raise ConfigurationError(
                'Could not identify gate type from gate_spec')

        return gate, duration

    def _parse_circuit(self, title, source, ordering, rng,
                       time_start, time_end):
        source_decomposed = list(self._expand_decompositions(source))
        gate_specs = [self._instructions[line] for line in source_decomposed]
        # Here we get all qubits, that actually participate in circuit
        qubits = set(chain(*(gs['qubits'] for gs in gate_specs)))
        circuit = ct.Circuit(title)
        for qubit in qubits:
            self._add_qubit(circuit, qubit)

        try:
            order_func = self._gates_order_table[ordering.lower()]
        except KeyError:
            raise RuntimeError('Unknown ordering: {}'.format(ordering))
        gates, tmin, tmax = order_func(
            qubits=qubits, gate_specs=gate_specs,
            gate_labels=source_decomposed, rng=rng,
            time_start=time_start, time_end=time_end)

        for gate in gates:
            circuit.add_gate(gate)

        # tmin might be important, tmax is defined by the last gate --
        # idling gates afterwards are useless
        circuit.add_waiting_gates(tmin=tmin, tmax=None)
        circuit.order()
        return circuit

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

    @staticmethod
    def _gate_is_single_qubit(gate_spec):
        out = (gate_spec['type'] != 'readout') and \
              (len(gate_spec['qubits']) == 1)
        if out:
            if len(gate_spec['matrix']) != 4:
                raise ConfigurationError(
                    'Process matrix is incompatible with number of qubits')
        return out

    @staticmethod
    def _gate_is_two_qubit(gate_spec):
        out = (gate_spec['type'] != 'readout') and \
              (len(gate_spec['qubits']) == 2)
        if out:
            if len(gate_spec['matrix']) != 16:
                raise ConfigurationError(
                    'Process matrix is incompatible with number of qubits')
        return out

    def _gates_order_alap(self, qubits, gate_specs, gate_labels,
                          rng, time_start, time_end):
        rng = ct._ensure_rng(rng)
        current_times = {qubit: 0. for qubit in qubits}
        gates = []
        for spec, label in zip(reversed(gate_specs),
                               reversed(gate_labels)):
            gate, duration = self._gate_spec_to_gate(spec, label, rng)
            # gate is validated already inside `_gate_spec_to_gate`
            if gate is None:
                continue
            gate_time_end = min((current_times[qubit]
                                 for qubit in spec['qubits']))
            gate_time_start = gate_time_end - duration
            gate.time = 0.5*(gate_time_start + gate_time_end)
            for qubit in spec['qubits']:
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
                gate.time += time_shift

        return gates, time_min + time_shift, time_shift

    def _gates_order_asap(self, qubits, gate_specs, gate_labels,
                          rng, time_start, time_end):
        rng = ct._ensure_rng(rng)
        current_times = {qubit: 0. for qubit in qubits}
        gates = []
        for spec, label in zip(gate_specs, gate_labels):
            gate, duration = self._gate_spec_to_gate(spec, label, rng)
            # gate is validated already inside `_gate_spec_to_gate`
            if gate is None:
                continue
            gate_time_start = max((current_times[qubit]
                                   for qubit in spec['qubits']))
            gate_time_end = gate_time_start + duration
            gate.time = 0.5*(gate_time_start + gate_time_end)
            for qubit in spec['qubits']:
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
                gate.time += time_shift
        return gates, time_shift, time_max + time_shift

    def _expand_decompositions(self, source):
        """Returns a generator of instructions from `source` (can be also a
        generator), expanding all aliases, specified in configuration in
        "gate_decompositions" field, so that we have only instructions from
        "instructions" field of configuration. Raises `QasmError`, if this is
        impossible.
        """
        for s in source:
            # FIXME Here we filter out prepz gates, based on name. Generally
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
                for s1 in self._expand_decompositions(maybe_decomposed):
                    yield s1
                continue
            raise QasmError("Unknown QASM instruction: {}".format(s))

    def _try_decompose(self, instr):
        """If instruction matches alias, this method returns expantion of
        instruction with this alias. If it does not match, it returns `None`.
        """
        for decomposer in self._decomposers:
            result = decomposer(instr)
            if result is not None:
                return result
        return None