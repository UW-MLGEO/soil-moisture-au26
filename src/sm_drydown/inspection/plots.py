"""Headless distribution figures, retaining tails and labeling sampling units."""
import numpy as np


def distribution_plot(series, path, title, units):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, len(series), figsize=(6*len(series), 4), squeeze=False)
    for ax, (label, sample) in zip(axes[0], series.items()):
        values = np.asarray(sample['values'], dtype=float)
        finite = values[np.isfinite(values)]
        ax.hist(finite, bins=100, color='#326d8c')
        if finite.size:
            ax.set_yscale('log')
        ax.set_ylabel('Source records (log count)')
        ax.set_xlabel(units)
        ax.set_title(f'{label}\nN={finite.size:,}; missing={sample["missing"]:,}')
        if not finite.size:
            ax.text(.5, .5, 'No finite observations', transform=ax.transAxes, ha='center')
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
