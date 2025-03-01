# Copyright 2023 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This module contains functions for computing the Hadamard-test gradient
of a qubit-based quantum tape.
"""
import pennylane as qml
import pennylane.numpy as np
from pennylane.measurements import MutualInfoMP, StateMP, VarianceMP, VnEntropyMP
from pennylane.transforms.metric_tensor import _get_aux_wire
from pennylane.transforms.tape_expand import expand_invalid_trainable_hadamard_gradient
from .finite_difference import _all_zero_grad_new, _no_trainable_grad_new

from .gradient_transform import (
    choose_grad_methods,
    grad_method_validation,
    gradient_analysis,
    gradient_transform,
)


def _hadamard_grad(
    tape,
    argnum=None,
    shots=None,
    aux_wire=None,
    device_wires=None,
):
    r"""Transform a QNode to compute the Hadamard test gradient of all gates with respect to their inputs.

    Args:
        tape (pennylane.QNode or .QuantumTape): quantum tape or QNode to differentiate
        argnum (int or list[int] or None): Trainable tape parameter indices to differentiate
            with respect to. If not provided, the derivatives with respect to all
            trainable parameters are returned.
        shots (None, int, list[int]): The device shots that will be used to execute the tapes outputted by this
            transform. Note that this argument doesn't influence the shots used for tape execution, but provides
            information about the shots.
        aux_wire (pennylane.wires.Wires): Auxiliary wire to be used for the Hadamard tests. If ``None`` (the default),
            a suitable wire is inferred from the wires used in the original circuit and ``device_wires``.
        device_wires (pennylane.wires.Wires): Wires of the device that are going to be used for the
            gradient. Facilitates finding a default for ``aux_wire`` if ``aux_wire``
            is ``None``.

    Returns:
        function or tuple[list[QuantumTape], function]:

        - If the input is a QNode, an object representing the Jacobian (function) of the QNode
          that can be executed to obtain the Jacobian.
          The type of the Jacobian returned is either a tensor, a tuple or a
          nested tuple depending on the nesting structure of the original QNode output.

        - If the input is a tape, a tuple containing a
          list of generated tapes, together with a post-processing
          function to be applied to the results of the evaluated tapes
          in order to obtain the Jacobian.

    For a variational evolution :math:`U(\mathbf{p}) \vert 0\rangle` with
    :math:`N` parameters :math:`\mathbf{p}`,
    consider the expectation value of an observable :math:`O`:

    .. math::

        f(\mathbf{p})  = \langle \hat{O} \rangle(\mathbf{p}) = \langle 0 \vert
        U(\mathbf{p})^\dagger \hat{O} U(\mathbf{p}) \vert 0\rangle.


    The gradient of this expectation value can be calculated via the Hadamard test gradient:

    .. math::

        \frac{\partial f}{\partial \mathbf{p}} = -2 \Im[\bra{0} \hat{O} G \ket{0}] = i \left(\bra{0} \hat{O} G \ket{
        0} - \bra{0} G\hat{O} \ket{0}\right) = -2 \bra{+}\bra{0} ctrl-G^{\dagger} (\hat{Y} \otimes \hat{O}) ctrl-G
        \ket{+}\ket{0}

    Here, :math:`G` is the generator of the unitary :math:`U`.

    **Example**

    This gradient transform can be applied directly to :class:`QNode <pennylane.QNode>` objects:

    >>> qml.enable_return()
    >>> dev = qml.device("default.qubit", wires=2)
    >>> @qml.qnode(dev)
    ... def circuit(params):
    ...     qml.RX(params[0], wires=0)
    ...     qml.RY(params[1], wires=0)
    ...     qml.RX(params[2], wires=0)
    ...     return qml.expval(qml.PauliZ(0))
    >>> params = np.array([0.1, 0.2, 0.3], requires_grad=True)
    >>> qml.gradients.hadamard_grad(circuit)(params)
    (tensor([-0.3875172], requires_grad=True),
     tensor([-0.18884787], requires_grad=True),
     tensor([-0.38355704], requires_grad=True))

    This quantum gradient transform can also be applied to low-level
    :class:`~.QuantumTape` objects. This will result in no implicit quantum
    device evaluation. Instead, the processed tapes, and post-processing
    function, which together define the gradient are directly returned:

    >>> with qml.tape.QuantumTape() as tape:
    ...     qml.RX(params[0], wires=0)
    ...     qml.RY(params[1], wires=0)
    ...     qml.RX(params[2], wires=0)
    ...     qml.expval(qml.PauliZ(0))
    >>> gradient_tapes, fn = qml.gradients.hadamard_grad(tape)
    >>> gradient_tapes
    [<QuantumTape: wires=[0, 1], params=3>,
     <QuantumTape: wires=[0, 1], params=3>,
     <QuantumTape: wires=[0, 1], params=3>]

    This can be useful if the underlying circuits representing the gradient
    computation need to be analyzed.

    The output tapes can then be evaluated and post-processed to retrieve
    the gradient:

    >>> dev = qml.device("default.qubit", wires=2)
    >>> fn(qml.execute(gradient_tapes, dev, None))
    (array(-0.3875172), array(-0.18884787), array(-0.38355704))

    This transform can be registered directly as the quantum gradient transform
    to use during autodifferentiation:

    >>> dev = qml.device("default.qubit", wires=3)
    >>> @qml.qnode(dev, interface="jax", diff_method="hadamard")
    ... def circuit(params):
    ...     qml.RX(params[0], wires=0)
    ...     qml.RY(params[1], wires=0)
    ...     qml.RX(params[2], wires=0)
    ...     return qml.expval(qml.PauliZ(0))
    >>> params = jax.numpy.array([0.1, 0.2, 0.3])
    >>> jax.jacobian(circuit)(params)
    [-0.3875172  -0.18884787 -0.38355704]

    If you use custom wires on your device, you need to pass an auxiliary wire.

    >>> qml.enable_return()
    >>> dev_wires = ("a", "c")
    >>> dev = qml.device("default.qubit", wires=dev_wires)
    >>> @qml.qnode(dev, interface="jax", diff_method="hadamard", aux_wire="c", device_wires=dev_wires)
    >>> def circuit(params):
    ...    qml.RX(params[0], wires="a")
    ...    qml.RY(params[1], wires="a")
    ...    qml.RX(params[2], wires="a")
    ...    return qml.expval(qml.PauliZ("a"))
    >>> params = jax.numpy.array([0.1, 0.2, 0.3])
    >>> jax.jacobian(circuit)(params)
    [-0.3875172  -0.18884787 -0.38355704]

    .. note::

        ``hadamard_grad`` will decompose the operations that are not in the list of supported operations.

        - ``pennylane.RX``
        - ``pennylane.RY``
        - ``pennylane.RZ``
        - ``pennylane.Rot``
        - ``pennylane.PhaseShift``
        - ``pennylane.U1``
        - ``pennylane.CRX``
        - ``pennylane.CRY``
        - ``pennylane.CRZ``
        - ``pennylane.IsingXX``
        - ``pennylane.IsingYY``
        - ``pennylane.IsingZZ``

        The expansion will fail if a suitable decomposition in terms of supported operation is not found.
        The number of trainable parameters may increase due to the decomposition.

    """
    if not qml.active_return():
        raise NotImplementedError(
            "The Hadamard gradient only supports the new return type. Use qml.enable_return() to turn it on."
        )
    if any(isinstance(m, VarianceMP) for m in tape.measurements):
        raise ValueError(
            "Computing the gradient of variances with the Hadamard test gradient is not implemented."
        )
    if any(isinstance(m, (StateMP, VnEntropyMP, MutualInfoMP)) for m in tape.measurements):
        raise ValueError(
            "Computing the gradient of circuits that return the state is not supported."
        )

    if argnum is None and not tape.trainable_params:
        return _no_trainable_grad_new(tape, shots)

    gradient_analysis(tape, grad_fn=hadamard_grad)
    method = "analytic"
    diff_methods = grad_method_validation(method, tape)

    if all(g == "0" for g in diff_methods):
        return _all_zero_grad_new(tape, shots)

    method_map = choose_grad_methods(diff_methods, argnum)

    argnum = [i for i, dm in method_map.items() if dm == "A"]

    if device_wires and len(tape.wires) == len(device_wires):
        raise qml.QuantumFunctionError("The device has no free wire for the auxiliary wire.")

    # Get default for aux_wire
    if aux_wire is None:
        aux_wire = _get_aux_wire(aux_wire, tape, device_wires)
    elif aux_wire[0] in tape.wires:
        raise qml.QuantumFunctionError("The auxiliary wire is already used.")
    elif aux_wire[0] not in device_wires:
        raise qml.QuantumFunctionError(
            "The requested auxiliary wire does not exist on the used device."
        )

    g_tapes, processing_fn = _expval_hadamard_grad(tape, argnum, aux_wire)

    return g_tapes, processing_fn


def _expval_hadamard_grad(tape, argnum, aux_wire):
    r"""Compute the Hadamard test gradient of a tape that returns an expectation value (probabilities are expectations
    values) with respect to a given set of all trainable gate parameters.
    The auxiliary wire is the wire which is used to apply the Hadamard gates and controlled gates.
    """
    # pylint: disable=too-many-statements
    argnums = argnum or tape.trainable_params
    g_tapes = []
    coeffs = []

    gradient_data = []
    measurements_probs = [
        idx
        for idx, m in enumerate(tape.measurements)
        if isinstance(m, qml.measurements.ProbabilityMP)
    ]
    for id_argnum, _ in enumerate(tape.trainable_params):
        if id_argnum not in argnums:
            # parameter has zero gradient
            gradient_data.append(0)
            continue

        trainable_op, idx, p_idx = tape.get_operation(id_argnum)

        ops_to_trainable_op = tape.operations[: idx + 1]
        ops_after_trainable_op = tape.operations[idx + 1 :]

        # Get a generator and coefficients
        sub_coeffs, generators = _get_generators(trainable_op)
        coeffs.extend(sub_coeffs)

        num_tape = 0

        for gen in generators:
            if isinstance(trainable_op, qml.Rot):
                # We only registered PauliZ as generator for Rot, therefore we need to apply some gates
                # before and after the generator for the first two parameters.
                if p_idx == 0:
                    # Move the Rot gate past the generator
                    op_before_trainable_op = ops_to_trainable_op.pop(-1)
                    ops_after_trainable_op = [op_before_trainable_op] + ops_after_trainable_op
                elif p_idx == 1:
                    # Apply additional rotations that effectively move the generator to the middle of Rot
                    ops_to_add_before = [
                        qml.RZ(-trainable_op.data[2], wires=trainable_op.wires),
                        qml.RX(np.pi / 2, wires=trainable_op.wires),
                    ]
                    ops_to_trainable_op.extend(ops_to_add_before)

                    ops_to_add_after = [
                        qml.RX(-np.pi / 2, wires=trainable_op.wires),
                        qml.RZ(trainable_op.data[2], wires=trainable_op.wires),
                    ]
                    ops_after_trainable_op = ops_to_add_after + ops_after_trainable_op

            ctrl_gen = [qml.ctrl(gen, control=aux_wire)]
            hadamard = [qml.Hadamard(wires=aux_wire)]
            ops = ops_to_trainable_op + hadamard + ctrl_gen + hadamard + ops_after_trainable_op

            measurements = []
            # Add the Y measurement on the aux qubit
            for m in tape.measurements:
                if isinstance(m.obs, qml.operation.Tensor):
                    obs_new = m.obs.obs.copy()
                elif m.obs:
                    obs_new = [m.obs]
                else:
                    obs_new = [qml.PauliZ(wires=i) for i in m.wires]

                obs_new.append(qml.PauliY(wires=aux_wire))
                obs_new = qml.operation.Tensor(*obs_new)

                if isinstance(m, qml.measurements.ExpectationMP):
                    measurements.append(qml.expval(op=obs_new))
                else:
                    measurements.append(qml.probs(op=obs_new))

            new_tape = qml.tape.QuantumScript(ops=ops, measurements=measurements)

            _rotations, _measurements = qml.tape.tape.rotations_and_diagonal_measurements(new_tape)
            # pylint: disable=protected-access
            new_tape._ops = new_tape._ops + _rotations
            new_tape._measurements = _measurements
            new_tape._update()

            num_tape += 1

            g_tapes.append(new_tape)

        gradient_data.append(num_tape)

    def processing_fn(results):  # pylint: disable=too-many-branches
        multi_measurements = len(tape.measurements) > 1
        multi_params = len(tape.trainable_params) > 1

        final_res = [
            [qml.math.convert_like(2 * coeff * r, r) for r in res]
            if isinstance(res, tuple)
            else qml.math.convert_like(2 * coeff * res, res)
            for coeff, res in zip(coeffs, results)
        ]

        # Post process for probs
        if measurements_probs:
            projector = np.array([1, -1])
            if multi_measurements:
                projector = qml.math.convert_like(projector, final_res[0][0])
            else:
                projector = qml.math.convert_like(projector, final_res[0])
            for idx, res in enumerate(final_res):
                if multi_measurements:
                    for prob_idx in measurements_probs:
                        num_wires_probs = len(tape.measurements[prob_idx].wires)
                        res_reshaped = qml.math.reshape(res[prob_idx], (2**num_wires_probs, 2))
                        final_res[idx][prob_idx] = qml.math.tensordot(
                            res_reshaped, projector, axes=[[1], [0]]
                        )
                else:
                    prob_idx = measurements_probs[0]
                    num_wires_probs = len(tape.measurements[prob_idx].wires)
                    res = qml.math.reshape(res, (2**num_wires_probs, 2))
                    final_res[idx] = qml.math.tensordot(res, projector, axes=[[1], [0]])
        grads = []
        idx = 0
        for num_tape in gradient_data:
            if num_tape == 0:
                grads.append(qml.math.zeros(()))
            elif num_tape == 1:
                grads.append(final_res[idx])
                idx += 1
            else:
                axis = None if not multi_measurements else 0
                grads.append(qml.math.sum(final_res[idx : idx + num_tape], axis=axis))
                idx += num_tape

        if not multi_measurements and not multi_params:
            return grads[0]

        if not (multi_params and multi_measurements):
            if multi_measurements:
                return tuple(grads[0])
            return tuple(grads)

        # Reordering to match the right shape for multiple measurements
        grads_reorder = [[0] * len(tape.trainable_params) for _ in range(len(tape.measurements))]

        for i in range(len(tape.measurements)):
            for j in range(len(tape.trainable_params)):
                grads_reorder[i][j] = grads[j][i]

        grads_tuple = tuple(tuple(elem) for elem in grads_reorder)

        return grads_tuple

    return g_tapes, processing_fn


def _get_generators(trainable_op):
    """From a trainable operation, extract the unitary generators and their coefficients. If an operation is added here
    one needs to also update the list of supported operation in the expand function given to the gradient transform.
    """
    # For PhaseShift, we need to separate the generator in two unitaries (Hardware compatibility)
    if isinstance(trainable_op, (qml.PhaseShift, qml.U1)):
        generators = [qml.PauliZ(wires=trainable_op.wires)]
        coeffs = [-0.5]
    elif isinstance(trainable_op, qml.CRX):
        generators = [
            qml.PauliX(wires=trainable_op.wires[1]),
            qml.prod(
                qml.PauliZ(wires=trainable_op.wires[0]), qml.PauliX(wires=trainable_op.wires[1])
            ),
        ]
        coeffs = [-0.25, 0.25]
    elif isinstance(trainable_op, qml.CRY):
        generators = [
            qml.PauliY(wires=trainable_op.wires[1]),
            qml.prod(
                qml.PauliZ(wires=trainable_op.wires[0]), qml.PauliY(wires=trainable_op.wires[1])
            ),
        ]
        coeffs = [-0.25, 0.25]
    elif isinstance(trainable_op, qml.CRZ):
        generators = [
            qml.PauliZ(wires=trainable_op.wires[1]),
            qml.prod(
                qml.PauliZ(wires=trainable_op.wires[0]), qml.PauliZ(wires=trainable_op.wires[1])
            ),
        ]
        coeffs = [-0.25, 0.25]
    elif isinstance(trainable_op, qml.IsingXX):
        generators = [
            qml.prod(
                qml.PauliX(wires=trainable_op.wires[0]), qml.PauliX(wires=trainable_op.wires[1])
            )
        ]
        coeffs = [-0.5]
    elif isinstance(trainable_op, qml.IsingYY):
        generators = [
            qml.prod(
                qml.PauliY(wires=trainable_op.wires[0]), qml.PauliY(wires=trainable_op.wires[1])
            )
        ]
        coeffs = [-0.5]
    elif isinstance(trainable_op, qml.IsingZZ):
        generators = [
            qml.prod(
                qml.PauliZ(wires=trainable_op.wires[0]), qml.PauliZ(wires=trainable_op.wires[1])
            )
        ]
        coeffs = [-0.5]
    # For rotation it is possible to only use PauliZ by applying some other rotations in the main function
    elif isinstance(trainable_op, qml.Rot):
        generators = [qml.PauliZ(wires=trainable_op.wires)]
        coeffs = [-0.5]
    else:
        generators = trainable_op.generator().ops
        coeffs = trainable_op.generator().coeffs

    return coeffs, generators


hadamard_grad = gradient_transform(
    _hadamard_grad, expand_fn=expand_invalid_trainable_hadamard_gradient
)
