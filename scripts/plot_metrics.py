"""
plot_metrics.py - builds report figures from real Rasa outputs.

Inputs (produced by the commands in README.md):
  tensorboard/            DIET training logs   (rasa train, tensorboard_log_directory in config.yml)
  results/v1_split/       baseline rasa test nlu output (before data augmentation)
  results/split/          rasa test nlu output after augmentation
  results/cv/             cross-validation output (rasa test nlu --cross-validation)
Outputs: docs/figures/*.png
Run:  python scripts/plot_metrics.py
"""
import glob
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = "docs/figures"
os.makedirs(OUT, exist_ok=True)
GREEN, AMBER, GREY = "#1e7a46", "#d08a1a", "#9aa8a0"


def training_curves(logdir="results/tb_full"):
    """Plot DIET epoch metrics (loss, intent accuracy, entity F1) from TensorBoard event files.
    Copy the event file of the final `rasa train` run into results/tb_full first (see README)."""
    import tensorflow as tf
    from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
    series = {}
    for f in glob.glob(f"{logdir}/events.out.tfevents.*"):
        ea = EventAccumulator(f, size_guidance={"tensors": 0}); ea.Reload()
        for tag in ("epoch_t_loss", "epoch_i_acc", "epoch_e_f1"):
            if tag in ea.Tags()["tensors"]:
                series[tag] = [(e.step, float(tf.make_ndarray(e.tensor_proto))) for e in ea.Tensors(tag)]
    names = {"epoch_t_loss": "total loss", "epoch_i_acc": "intent accuracy", "epoch_e_f1": "entity F1"}
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.4))
    if "epoch_t_loss" in series:
        xs, ys = zip(*series["epoch_t_loss"]); axes[0].plot(xs, ys, color=GREEN, lw=1.6)
    axes[0].set_title("DIET training loss"); axes[0].set_xlabel("epoch")
    for tag, col in (("epoch_i_acc", GREEN), ("epoch_e_f1", AMBER)):
        if tag in series:
            xs, ys = zip(*series[tag]); axes[1].plot(xs, ys, color=col, lw=1.6, label=names[tag])
    axes[1].set_title("DIET training accuracy / entity F1"); axes[1].set_xlabel("epoch"); axes[1].set_ylim(0, 1.02)
    axes[1].legend(loc="lower right", fontsize=8)
    for ax in axes: ax.grid(alpha=.3)
    plt.tight_layout(); plt.savefig(f"{OUT}/fig_training_curves.png", dpi=170); plt.close()
    return {k: v[-1][1] for k, v in series.items()}


def f1_by_intent():
    """Per-intent F1 before and after data augmentation (same 80/20 seed)."""
    def load(p):
        r = json.load(open(p))
        return {k: v["f1-score"] for k, v in r.items() if isinstance(v, dict) and "avg" not in k and "f1-score" in v}
    v1, v2 = load("results/v1_split/intent_report.json"), load("results/split/intent_report.json")
    v2 = {k: v for k, v in v2.items()}
    intents = sorted(v2, key=lambda k: v2[k])
    y = range(len(intents))
    fig, ax = plt.subplots(figsize=(8, 6))
    ax.barh([i + 0.2 for i in y], [v1.get(k, 0) for k in intents], height=0.4, color=GREY, label="v1 baseline")
    ax.barh([i - 0.2 for i in y], [v2[k] for k in intents], height=0.4, color=GREEN, label="v2 augmented")
    ax.axvline(0.85, color=AMBER, linestyle="--", label="target F1 = 0.85")
    ax.set_yticks(list(y)); ax.set_yticklabels(intents); ax.set_xlim(0, 1.05); ax.set_xlabel("F1 score (held-out 20%)")
    ax.legend(loc="lower right", fontsize=8); ax.grid(axis="x", alpha=.3)
    plt.tight_layout(); plt.savefig(f"{OUT}/fig_f1_by_intent.png", dpi=170); plt.close()


if __name__ == "__main__":
    print("final epoch metrics:", training_curves())
    f1_by_intent()
    for src, dst in [("results/split/intent_confusion_matrix.png", "fig_confusion_matrix.png")]:
        if os.path.exists(src):
            import shutil; shutil.copy(src, f"{OUT}/{dst}")
    print("figures written to", OUT)
