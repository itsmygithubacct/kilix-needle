"""Train the job router (router.py) from the files its manifest lists.

    python3 router_train.py            retrain; write router-model/router.json and
                                       refresh the manifest's digests and counts
    python3 router_train.py --check    retrain in memory and compare with the
                                       checked-in artifact (exit 1 on a difference)
    python3 router_train.py --select   grouped 5-fold cross-validation; report the
                                       selective curve and the threshold rule's choice
                                       (router-model/selection.json)

Training needs NumPy (inference does not). It runs single-threaded and is
deterministic: the vocabulary is sorted, the optimiser is a fixed L-BFGS, and
weights are rounded to 4 decimals before export. A bit-exact digest across CPUs
is not promised (BLAS dot products may round differently), so --check compares
every weight within 1e-4 and the predictions on evals/router/probe.jsonl.

Sources are only the files the manifest names, and never a job's current gate
set (jobs.py `heldout`): such a path is refused before it is opened.
"""
from __future__ import annotations

import os

for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

import jobs
import router

HERE = Path(__file__).resolve().parent
MANIFEST = router.MODEL_DIR / "manifest.json"
ARTIFACT = router.MODEL_DIR / "router.json"
PROBE = HERE / "evals" / "router" / "probe.jsonl"
GATES = frozenset(job.heldout for job in jobs.JOBS.values())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lines(path: Path):
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                yield number, json.loads(line)


def _identity(path) -> tuple:
    """Where a path really is, without opening it: its resolved name and inode."""
    real = os.path.realpath(path)
    try:
        info = os.stat(real)
        return real, (info.st_dev, info.st_ino)
    except OSError:
        return real, None


def refuse_gates(manifest: dict) -> None:
    """Refuse a manifest naming a gate set, as a source or as relabels, under
    any spelling (./, an absolute path, a symlink or a hard link), before any
    file it names is opened or hashed."""
    gates = [_identity(HERE / gate) for gate in GATES]
    names = {real for real, _inode in gates}
    inodes = {inode for _real, inode in gates if inode}
    named = [source["path"] for source in manifest["sources"]]
    if manifest.get("relabels"):
        named.append(manifest["relabels"]["path"])
    for path in named:
        real, inode = _identity(HERE / path)
        if real in names or inode in inodes:
            raise SystemExit(f"router_train: {path} is a current gate set and is never read")


def load_rows(manifest: dict) -> list:
    """[(source, label, request)], relabelled and de-duplicated by normalised text."""
    refuse_gates(manifest)
    relabel = {}
    if manifest.get("relabels"):
        for _n, row in _lines(HERE / manifest["relabels"]["path"]):
            relabel[(row["file"], router.norm(row["request"]))] = row["new"]
    rows, seen = [], {}
    for source in manifest["sources"]:
        path = source["path"]
        for _n, case in _lines(HERE / path):
            if source["rule"] == "eval":
                # An eval row with calls is its job's; a "none"-tagged row is no
                # job's; bait and refusal rows have no route of their own.
                if case.get("expect"):
                    label = source["job"]
                elif case.get("tag") == "none":
                    label = "none"
                else:
                    continue
                label = relabel.get((path, router.norm(case["request"])), label)
            else:
                label = case["job"]
            if label not in router.LABELS:
                raise SystemExit(f"router_train: {path}: unknown label {label!r}")
            key = router.norm(case["request"])
            if key in seen:
                if seen[key] != label:
                    raise SystemExit(f"router_train: {path}: {case['request']!r} is labelled "
                                     f"both {seen[key]} and {label}")
                continue
            seen[key] = label
            rows.append((path, label, case["request"]))
    return rows


# ---- features ---------------------------------------------------------------
def vocabulary(texts: list, min_df: int) -> tuple:
    df: dict = {}
    for text in texts:
        for key in router.features(text):
            df[key] = df.get(key, 0) + 1
    keys = sorted(k for k, v in df.items() if v >= min_df)
    n = len(texts)
    idf = {k: round(math.log((1 + n) / (1 + df[k])) + 1, 6) for k in keys}
    return keys, idf


def matrix(texts: list, keys: list, idf: dict):
    """CSR arrays (data, indices, indptr) of the L2-normalised log-TF x IDF rows."""
    import numpy as np
    index = {k: i for i, k in enumerate(keys)}
    data, indices, indptr = [], [], [0]
    for text in texts:
        cols = sorted((index[k], (1.0 + math.log(c)) * idf[k])
                      for k, c in router.features(text).items() if k in index)
        length = math.sqrt(sum(v * v for _, v in cols)) or 1.0
        indices.extend(i for i, _ in cols)
        data.extend(v / length for _, v in cols)
        indptr.append(len(indices))
    return (np.array(data, dtype=np.float64), np.array(indices, dtype=np.int64),
            np.array(indptr, dtype=np.int64), len(keys))


class Sparse:
    """X and its transpose, with products that sum in a fixed order."""

    def __init__(self, csr):
        import numpy as np
        self.data, self.indices, self.indptr, self.d = csr
        self.n = len(self.indptr) - 1
        rows = np.repeat(np.arange(self.n), np.diff(self.indptr))
        order = np.lexsort((rows, self.indices))
        self.t_data, self.t_rows = self.data[order], rows[order]
        counts = np.bincount(self.indices, minlength=self.d)
        self.t_ptr = np.concatenate([[0], np.cumsum(counts)])

    @staticmethod
    def _segments(values, ptr, length):
        import numpy as np
        out = np.zeros((length, values.shape[1]))
        nonempty = np.flatnonzero(np.diff(ptr))
        if len(nonempty):
            out[nonempty] = np.add.reduceat(values, ptr[nonempty], axis=0)
        return out

    def dot(self, w):           # X @ W
        return self._segments(self.data[:, None] * w[self.indices], self.indptr, self.n)

    def tdot(self, g):          # X.T @ G
        return self._segments(self.t_data[:, None] * g[self.t_rows], self.t_ptr, self.d)


# ---- class-balanced multinomial logistic regression, L2, own L-BFGS --------
def fit(x: Sparse, y, k: int, c: float, *, max_iter: int, gtol: float):
    import numpy as np
    n, d = x.n, x.d
    counts = np.bincount(y, minlength=k).astype(np.float64)
    weights = (n / (k * np.maximum(counts, 1)))[y]
    onehot = np.eye(k)[y]

    def loss(theta):
        w = theta[:d * k].reshape(d, k)
        b = theta[d * k:]
        z = x.dot(w) + b
        z -= z.max(axis=1, keepdims=True)
        p = np.exp(z)
        p /= p.sum(axis=1, keepdims=True)
        nll = -(weights * np.log(np.maximum((p * onehot).sum(axis=1), 1e-300))).sum()
        g = (p - onehot) * weights[:, None]
        grad = np.concatenate([(x.tdot(g) + w / c).ravel(), g.sum(axis=0)])
        return nll + 0.5 / c * float((w * w).sum()), grad

    theta = lbfgs(loss, np.zeros(d * k + k), max_iter=max_iter, gtol=gtol)
    return theta[:d * k].reshape(d, k), theta[d * k:]


def lbfgs(f, x, *, memory: int = 10, max_iter: int, gtol: float):
    """Minimise a smooth convex f (returning value, gradient) to max|g| <= gtol."""
    import numpy as np
    fx, g = f(x)
    s_hist, y_hist = [], []
    for _ in range(max_iter):
        if float(np.abs(g).max()) <= gtol:
            break
        q = g.copy()
        alphas = []
        for s, y in zip(reversed(s_hist), reversed(y_hist)):
            rho = 1.0 / float(y @ s)
            a = rho * float(s @ q)
            q -= a * y
            alphas.append((rho, a))
        if s_hist:
            q *= float(s_hist[-1] @ y_hist[-1]) / float(y_hist[-1] @ y_hist[-1])
        else:
            q /= max(1.0, float(np.abs(g).max()))
        for (s, y), (rho, a) in zip(zip(s_hist, y_hist), reversed(alphas)):
            q += s * (a - rho * float(y @ q))
        d = -q
        slope = float(g @ d)
        if slope >= 0:              # not a descent direction: restart from the gradient
            s_hist, y_hist = [], []
            d, slope = -g, -float(g @ g)
        step = 1.0
        while True:
            xn = x + step * d
            fn, gn = f(xn)
            if fn <= fx + 1e-4 * step * slope:
                break
            step *= 0.5
            if step < 1e-20:
                return x
        s, y = xn - x, gn - g
        if float(y @ s) > 1e-12:
            s_hist.append(s)
            y_hist.append(y)
            if len(s_hist) > memory:
                s_hist.pop(0)
                y_hist.pop(0)
        x, fx, g = xn, fn, gn
    return x


def train(rows: list, settings: dict) -> dict:
    """The artifact for these rows: labels, idf, rounded weights, bias, threshold."""
    import numpy as np
    texts = [r[2] for r in rows]
    y = np.array([router.LABELS.index(r[1]) for r in rows])
    keys, idf = vocabulary(texts, settings["min_df"])
    x = Sparse(matrix(texts, keys, idf))
    w, b = fit(x, y, len(router.LABELS), settings["c"],
               max_iter=settings["max_iter"], gtol=settings["gtol"])
    places = settings["round"]
    weights = {}
    for i, key in enumerate(keys):
        row = [round(float(v), places) + 0.0 for v in w[i]]
        if any(row):
            weights[key] = row
    return {"schema": "kilix-needle.router/v1", "labels": list(router.LABELS), "idf": idf,
            "weights": weights, "bias": [round(float(v), places) + 0.0 for v in b],
            "threshold": settings["threshold"]}


def dump(artifact: dict) -> bytes:
    return (json.dumps(artifact, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def probe_texts() -> list:
    return [row["request"] for _n, row in _lines(PROBE)]


def check(manifest: dict) -> int:
    """Retrain from the manifest and compare with the checked-in artifact."""
    refuse_gates(manifest)
    for source in manifest["sources"] + ([manifest["relabels"]] if manifest.get("relabels") else []):
        if _sha(HERE / source["path"]) != source["sha256"]:
            print(f"router_train: {source['path']} differs from its manifest digest")
            return 1
    shipped = json.loads(ARTIFACT.read_bytes())
    if _sha(ARTIFACT) != manifest["artifact"]["sha256"]:
        print("router_train: router.json differs from its manifest digest")
        return 1
    fresh = train(load_rows(manifest), manifest["trainer"])
    problems = []
    if fresh["idf"] != shipped["idf"]:
        problems.append("idf differs")
    for key in set(fresh["weights"]) | set(shipped["weights"]):
        a = fresh["weights"].get(key, [0.0] * len(router.LABELS))
        b = shipped["weights"].get(key, [0.0] * len(router.LABELS))
        if any(abs(u - v) > 1e-4 + 1e-12 for u, v in zip(a, b)):
            problems.append(f"weight {key!r} differs by more than 1e-4")
            break
    if any(abs(u - v) > 1e-4 + 1e-12 for u, v in zip(fresh["bias"], shipped["bias"])):
        problems.append("bias differs by more than 1e-4")
    if fresh["threshold"] != shipped["threshold"]:
        problems.append("threshold differs")
    old, new = router.Model(shipped), router.Model(fresh)
    for text in probe_texts():
        if old.rank(text)[0][0] != new.rank(text)[0][0]:
            problems.append(f"probe {text!r} predicts differently")
            break
    for problem in problems:
        print(f"router_train: {problem}")
    if not problems:
        print(f"router_train: retrained artifact matches ({len(shipped['weights'])} features)")
    return 1 if problems else 0


def write(manifest: dict) -> None:
    rows = load_rows(manifest)
    artifact = dump(train(rows, manifest["trainer"]))
    ARTIFACT.write_bytes(artifact)
    for source in manifest["sources"]:
        path = HERE / source["path"]
        source["sha256"] = _sha(path)
        source["rows"] = sum(1 for _ in _lines(path))
    if manifest.get("relabels"):
        manifest["relabels"]["sha256"] = _sha(HERE / manifest["relabels"]["path"])
    manifest["rows"] = {label: sum(1 for r in rows if r[1] == label) for label in router.LABELS}
    manifest["artifact"] = {"path": "router-model/router.json",
                            "sha256": hashlib.sha256(artifact).hexdigest(), "bytes": len(artifact)}
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(f"router_train: {len(rows)} rows, {manifest['rows']}, artifact {len(artifact)} bytes")


# ---- threshold selection -----------------------------------------------------
def folds(rows: list, rules: dict, k: int = 5) -> list:
    """Group 5-fold assignment. Each job eval file and each spent router
    held-out set (rule "spent") was written as a separate generation and stays
    whole, so those rows measure transfer to new phrasing. A router file (rule
    "labelled") holds most of some label's rows, so it is split into k chunks by
    a hash of the text; otherwise a fold would hold out a whole label."""
    groups = []
    for source, _label, text in rows:
        if rules[source] == "labelled":
            chunk = int(hashlib.sha256(router.norm(text).encode()).hexdigest(), 16) % k
            groups.append(f"{source}#{chunk}")
        else:
            groups.append(source)
    order = sorted(set(groups))
    fold_of = {g: i % k for i, g in enumerate(order)}
    return [fold_of[g] for g in groups]


def select(manifest: dict, target: float) -> dict:
    import numpy as np
    rows = load_rows(manifest)
    assignment = folds(rows, {source["path"]: source["rule"] for source in manifest["sources"]})
    settings = dict(manifest["trainer"], threshold=0.5)
    pred, conf = [None] * len(rows), np.zeros(len(rows))
    for fold in range(5):
        test = [i for i, f in enumerate(assignment) if f == fold]
        held = {router.norm(rows[i][2]) for i in test}
        train_rows = [r for i, r in enumerate(rows) if assignment[i] != fold
                      and router.norm(r[2]) not in held]
        model = router.Model(train(train_rows, settings))
        for i in test:
            label, p = model.rank(rows[i][2])[0]
            pred[i], conf[i] = label, p
        print(f"fold {fold}: {len(test)} rows", file=sys.stderr, flush=True)
    labels = [r[1] for r in rows]
    ok = np.array([p == t for p, t in zip(pred, labels)])
    order = np.argsort(-conf, kind="stable")
    curve = []
    for coverage in (1.0, 0.98, 0.95, 0.9, 0.85, 0.8):
        take = order[:int(round(coverage * len(rows)))]
        curve.append({"coverage": coverage, "accuracy": float(ok[take].mean()),
                      "threshold": float(conf[take[-1]])})
    # Rule: the lowest probability whose accepted rows are at least `target`
    # accurate, rounded up to 0.01, within [0.5, 0.95]. Frozen 2026-10-19 before
    # any router held-out row was read; round 2 moved the target from 99.5% to
    # 98.5% (README "Threshold"), before any round-2 held-out row was read.
    threshold = 0.95
    for t in [x / 100 for x in range(50, 96)]:
        accepted = conf >= t
        if accepted.any() and ok[accepted].mean() >= target:
            threshold = t
            break
    accepted = conf >= threshold
    recall = {label: float(ok[[i for i, t in enumerate(labels) if t == label]].mean())
              for label in router.LABELS if label in labels}
    errors = [{"file": rows[i][0], "request": rows[i][2], "job": labels[i], "predicted": pred[i],
               "p": round(float(conf[i]), 3)} for i in np.flatnonzero(~ok)]
    return {"rows": len(rows), "accuracy": float(ok.mean()),
            "macro_recall": float(np.mean(list(recall.values()))), "recall": recall,
            "target": target, "threshold": threshold, "coverage": float(accepted.mean()),
            "accepted_accuracy": float(ok[accepted].mean()), "selective": curve,
            "errors": sorted(errors, key=lambda e: -e["p"])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--select", action="store_true")
    parser.add_argument("--target", type=float, default=0.985)
    args = parser.parse_args(argv)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if args.check:
        return check(manifest)
    if args.select:
        report = select(manifest, args.target)
        (router.MODEL_DIR / "selection.json").write_text(json.dumps(report, indent=1) + "\n",
                                                         encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k not in ("errors",)}, indent=1))
        return 0
    write(manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
