"""Shared heatmap axes styling for the viewer and PNG exports."""
from matplotlib.ticker import AutoMinorLocator, MaxNLocator


def style_heatmap_axes(ax):
    ax.grid(False, which="both")
    ax.set_axisbelow("line")
    for axis in (ax.xaxis, ax.yaxis):
        axis.set_major_locator(MaxNLocator(nbins=5, min_n_ticks=4))
        axis.set_minor_locator(AutoMinorLocator(2))
    ax.tick_params(which="major", direction="in", top=True, right=True, length=5)
    ax.tick_params(which="minor", direction="in", top=True, right=True, length=2)
