import numpy as np
from itertools import product


def plot(
    state,
    *,
    ax=None,
    truncate_levels=None,
    colorbar=True,
    amp_limits=None,
    phase_limits=None,
    cmap_name="plasma"
):
    """
    Plots the density matrix as a complex 3D histogram.

    Parameters
    ----------
    state : qs.State
        State to display
    ax : matplotlib.axes.Axes or None
        Axes to plot onto. If None, new figure is created and returned.
    truncate_levels : None or int
        If not None, all the states higher than provided are discarded and a
        identity is added to the state instead, so that total trace is
        preserved. This should emulate behaviour of tomography in the presence
        of leakage.
    colorbar : bool, optional
        If True, a colorbar is created and drawn to the figure axes, by default True
    amp_limits : list or tuple or None
        A list or tuple of two float numbers, corresponding to the lower and upper
        limit of the z-axis, in this case corresponding to the state amplitude.
        If None, the lower limit is set to 0, while the upper one to 1.
    phase_limits : list or tuple or None
        A list or tuple of two float numbers, corresponding to the lower and upper
        limit of phase-axis (the colorbar), in this case corresponding to the complex
        phase of the state.
        If None, the lower limit is set to :math:`-\\pi`, while the upper one to
        :math:`\\pi`.
    cmap_name : str
        Name of the colormap to use to plot phase.

    Returns
    -------
    fig : matplotlib.figure.Figure or None
    """
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize
    from matplotlib import colorbar as colorbar_

    if ax is None:
        fig, ax = plt.subplots(subplot_kw=dict(projection="3d"))
    else:
        fig = None

    if cmap_name not in plt.colormaps():
        raise ValueError(
            "The given colormap name is not valid, please provide the name of"
            " one of the standard built-in colormaps in matplotlib"
        )

    n_qubits = len(state.qubits)
    _rho = state.to_dm()
    _rho /= np.trace(_rho)

    if truncate_levels is not None:
        # Tomo emulation: truncate leaked states and add
        rho = _rho.reshape(state.pauli_vector.dim_hilbert * 2)[
            (slice(0, truncate_levels),) * (2 * n_qubits)
        ].reshape(truncate_levels**n_qubits, truncate_levels**n_qubits)
        trace = np.trace(rho)
        rho += (1 - trace) * np.identity(2**n_qubits) * truncate_levels**-n_qubits
        assert np.allclose(np.trace(rho), 1)
        dim = truncate_levels
    else:
        dim = state.dim_hilbert
        rho = _rho

    def tuple_to_string(tup):
        state_ = "".join(str(x) for x in tup)
        return r"$\left| %s \right\rangle$" % state_

    labels = [
        tuple_to_string(x) for x in product(*(range(dim) for _ in range(n_qubits)))
    ]

    if phase_limits and isinstance(phase_limits, (list, tuple)):
        assert len(phase_limits) == 2
    else:
        phase_limits = (-np.pi, np.pi)

    norm = Normalize(*phase_limits)
    cmap = plt.get_cmap(cmap_name)
    colors = cmap(norm(np.angle(rho.flatten())))

    if amp_limits and isinstance(phase_limits, (list, tuple)):
        assert len(amp_limits) == 2
    else:
        amp_limits = (0, 1)

    xpos, ypos = np.meshgrid(*(range(dim) for dim in rho.shape))
    xpos = xpos.flatten()
    ypos = ypos.flatten()
    zpos = 0
    dx = dy = 0.5 * np.ones(rho.size)
    dz = np.abs(np.real(rho.flatten()))

    ax.bar3d(xpos, ypos, zpos, dx, dy, dz, color=colors)
    ax.set_zlim3d(amp_limits)

    ax.axes.w_xaxis.set_major_locator(plt.IndexLocator(1, 0.25))
    ax.set_xticklabels(labels)

    ax.axes.w_yaxis.set_major_locator(plt.IndexLocator(1, 0.25))
    ax.set_yticklabels(labels)

    plt.setp(
        ax.get_xticklabels(),
        rotation=45,
        ha="center",
        va="baseline",
        rotation_mode="anchor",
    )

    plt.setp(
        ax.get_yticklabels(),
        rotation=-45,
        ha="center",
        va="baseline",
        rotation_mode="anchor",
    )

    ax.set_zlabel("Amplitude")

    if colorbar:
        cax, _ = colorbar_.make_axes(ax)
        cb = colorbar_.ColorbarBase(cax, cmap=cmap, norm=norm)
        cb.set_ticks((-np.pi, 0, np.pi))
        cb.set_ticklabels((r"$-\pi$", r"$0$", r"$\pi$"))
        cb.set_label("Phase")

    return fig
