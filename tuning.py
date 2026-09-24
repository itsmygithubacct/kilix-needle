"""Fine-tune Needle 2 from kilix-ml's kilix_panes domain pack, the tuning library.

    kilix-needle tune [--base-dir DIR] [--library FILE] [--steps-only] [--background]
    kilix-needle tune --status | --select RUN_DIR | --deselect

A run works in its own directory and leaves a marker after each stage, so an
interrupted run resumes where it stopped:

  base     the checkpoint and tokenizer, verified against the manifest digests
           (the checkpoint is a pickle and is never read before that)
  source   Needle's training code at the pinned commit, fetched once
  env      a Python environment from the hash-locked requirements
  data     examples generated from the library plus kilix-needle's blind
           supplement, each checked against the same rules the running tool
           applies, minus every eval request, no action above its share cap
  train    quantisation-aware LoRA fine-tuning (see RECIPE), offline (its own
           network namespace when the kernel allows one), at the lowest CPU
           priority
  export   the tuned .cact
  gates    the benchmarks: no unsafe action on any eval set, and a clear gain
           on the held-out set written without sight of the library
  select   only if every gate passed: the tuned model becomes the one used

Nothing here accepts a licence: the base files come from installed content
assets (or --base-dir for development, held to the same digests).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

REPO = Path(__file__).resolve().parent


def library_path() -> Path:
    """kilix-ml's kilix_panes pack: the tuning library has one source, kilix-ml.

    KILIX_ML_HOME names a kilix-ml checkout (a workspace's live copy);
    otherwise the pinned third_party/kilix-ml submodule is used.
    """
    configured = os.environ.get("KILIX_ML_HOME")
    ml = Path(configured).expanduser() if configured else REPO / "third_party" / "kilix-ml"
    return ml / "domains" / "kilix_panes"


LIBRARY = library_path()
APP_HOME = Path(os.environ.get("GPU_TERMINAL_HOME") or Path.home() / ".local" / "gpu_terminal") \
    / "kilix-apps" / "kilix-needle"
SELECTION = APP_HOME / "model.json"
SUPPLEMENT = REPO / "corpus-supplement"

# kilix-needle's recipe, applied over kilix-ml's manifest.toml (kilix-ml owns
# the pack; what was learned about training Needle lives here).
#   qat         train through the checkpoint's own quantiser. Upstream trains
#               in float and export quantises to 2 bits; measured on run 2, the
#               quantiser's error was ~50x the adapter's change and the tuned
#               model lost to the base. QAT runs 3-5 beat it on every set.
#   epochs      4: the QAT runs reached their best validation loss there.
#   supplements templates written blind (no sight of any eval set) for the
#               phrasings the pack lacks; run 4 needed them for resize.
#   cap_share   no one action above this share of the examples: resize at 24%
#               cost open_tab 3 cases (run 4); at 12% resize lost 3 (run 5).
#   heldout     the newest set, which no decision about the model has seen.
RECIPE = {"train": {"epochs": 4, "qat": True},
          "data": {"supplements": [SUPPLEMENT], "cap_share": 0.18},
          "gates": {"heldout": "evals/heldout-v8.jsonl"}}
STAGES = ("base", "source", "env", "data", "train", "export", "gates", "select")


class TuneError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(library: Path = LIBRARY) -> dict:
    path = library / "manifest.toml"
    if not path.exists():
        raise TuneError(f"no kilix-ml tuning library at {library}; run `git submodule "
                        "update --init third_party/kilix-ml`, or set KILIX_ML_HOME to a "
                        "kilix-ml checkout")
    manifest = tomllib.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "kilix-needle-tuning/v1":
        raise TuneError(f"unsupported tuning manifest {manifest.get('schema')!r}")
    return manifest


def recipe(manifest: dict) -> dict:
    """The manifest with kilix-needle's recipe applied, excluding every eval set."""
    merged = {key: dict(value) if isinstance(value, dict) else value
              for key, value in manifest.items()}
    for section, values in RECIPE.items():
        merged.setdefault(section, {}).update(values)
    evals = sorted(str(p.relative_to(REPO)) for p in (REPO / "evals").glob("*.jsonl"))
    merged["data"]["exclude"] = sorted(set(merged["data"].get("exclude", [])) | set(evals))
    return merged


class Run:
    """One tuning run's directory, log and stage markers."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log_path = root / "tune.log"

    def log(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
        print(f"  {message}", flush=True)

    def done(self, stage: str) -> bool:
        return (self.root / f".{stage}.done").exists()

    def mark(self, stage: str, detail: dict | None = None) -> None:
        (self.root / f".{stage}.done").write_text(json.dumps(detail or {}), encoding="utf-8")


def _verify(path: Path, sha: str, what: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise TuneError(f"{what} is missing: {path}")
    actual = sha256_file(path)
    if actual != sha:
        raise TuneError(f"{what} does not match its pinned digest ({actual[:12]}…); not used")


def stage_base(run: Run, manifest: dict, base_dir: Path | None) -> None:
    """Checkpoint and tokenizer, verified, copied into the run."""
    base = manifest["base"]
    if base_dir is None:
        import asset
        base_dir = Path(asset.installed_asset_dir(base["checkpoint_asset"]))
    files = {"checkpoints/needle2.pkl": base["checkpoint_sha256"],
             "tokenizer/tokenizer.model": base["tokenizer_model_sha256"],
             "tokenizer/tokenizer.vocab": base["tokenizer_vocab_sha256"]}
    target = run.root / "base"
    for rel, sha in files.items():
        source = base_dir / rel
        _verify(source, sha, rel)
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target / rel)
        _verify(target / rel, sha, rel)
    run.log("base: checkpoint and tokenizer verified")


def _git(*args: str, cwd: Path) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=900)
    if done.returncode != 0:
        raise TuneError(f"git {args[0]}: {done.stderr.strip()[-300:]}")
    return done.stdout.strip()


def stage_source(run: Run, manifest: dict) -> None:
    """The training code at exactly the pinned commit, with the base files placed."""
    source = manifest["source"]
    src = run.root / "src"
    if not (src / ".git").exists():
        src.mkdir(parents=True, exist_ok=True)
        _git("init", "-q", cwd=src)
        run.log(f"source: fetching {source['url']} {source['tag']}")
        _git("fetch", "-q", "--depth", "1", source["url"], source["commit"], cwd=src)
        _git("checkout", "-q", "--detach", "FETCH_HEAD", cwd=src)
    head = _git("rev-parse", "HEAD", cwd=src)
    if head != source["commit"]:
        raise TuneError(f"source is at {head}, not the pinned {source['commit']}")
    if _git("status", "--porcelain", "--untracked-files=no", cwd=src):
        raise TuneError("the source checkout has local changes")
    # Place the verified base files where upstream looks, so it never downloads them.
    (src / "checkpoints").mkdir(exist_ok=True)
    shutil.copyfile(run.root / "base/checkpoints/needle2.pkl", src / "checkpoints/needle2.pkl")
    for name in ("tokenizer.model", "tokenizer.vocab"):
        shutil.copyfile(run.root / "base/tokenizer" / name, src / "needle/model" / name)
    run.log(f"source: {head[:12]} checked out, base files placed")


def stage_env(run: Run, manifest: dict, library: Path) -> None:
    env_dir = run.root / "env"
    lock = library / manifest["environment"]["requirements"]
    if shutil.which("uv") is None:
        raise TuneError("uv is not installed; it builds the training environment")
    subprocess.run(["uv", "venv", "-q", "-p", manifest["environment"]["python"], str(env_dir)],
                   check=True, timeout=900)
    subprocess.run(["uv", "pip", "install", "-q", "--require-hashes", "--python",
                    str(env_dir / "bin" / "python"), "-r", str(lock)], check=True, timeout=3600)
    run.log(f"env: {manifest['environment']['requirements']} installed with hashes")


_INTENT = {"open": "open a {kind}", "close": "close a {kind}", "go_to": "go to a {kind}",
           "adjust": "change the current tab", "run_in_pane": "type a command into a pane"}
_NO_TOOL = ("No tool fits: the request does not ask to open, close, go to, change or "
            "type into a pane or tab.")


def reasoning_for(calls: list[dict], spans: list) -> str:
    """The derivation Needle writes before its calls, in the base model's style.

    Upstream trains `<think>reasoning</think>` then the calls. Run 1 left the
    reasoning empty; the tuned model learned to skip it and filled arguments
    with schema words ("narrower", "adjust") instead of the request's.
    """
    if not calls:
        return _NO_TOOL
    words = {str(value): surface for surface, value in spans}
    parts = []
    for call in calls:
        args = call["arguments"]
        intent = _INTENT.get(call["name"], call["name"]).format(kind=args.get("kind", "pane"))
        derived = []
        for key, value in args.items():
            if key == "kind":
                continue
            source = words.get(str(value))
            derived.append(f"'{source}' -> {key} '{value}'" if source else f"{key} '{value}'")
        parts.append(f"User wants to {intent}." + (" " + "; ".join(derived) + "." if derived else ""))
    return " ".join(parts)


def load_generator(library: Path):
    # Do not use sys.path + `import generate`: a prior validation run may have
    # cached a different pack under that generic module name.
    spec = importlib.util.spec_from_file_location("kilix_needle_corpus", library / "generate.py")
    if spec is None or spec.loader is None:
        raise TuneError(f"cannot load the generator at {library}")
    corpus = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(corpus)
    return corpus


def _typed(kind: str, args: dict) -> dict:
    """A library binds numbers as digit strings; give integer parameters integers.

    Measured: the kilix-ml pack's move_tab templates bind position "8", and
    the check (rightly) refuses a string where the schema says integer.
    """
    from actions import TOOLS
    schema = next((t["parameters"]["properties"] for t in TOOLS if t["name"] == kind), {})
    return {key: int(value) if schema.get(key, {}).get("type") == "integer"
            and isinstance(value, str) and value.isdigit() else value
            for key, value in args.items()}


def stage_library(library: Path, supplements: list, target: Path) -> list[str]:
    """A copy of the pack with the supplement templates beside its own."""
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(library, target, ignore=shutil.ignore_patterns("__pycache__"))
    added = []
    for supplement in supplements:
        for path in sorted((Path(supplement) / "actions").glob("*.json")):
            destination = target / "corpus" / "actions" / path.name
            if destination.exists():
                raise TuneError(f"supplement {path.name} would replace a pack template")
            shutil.copyfile(path, destination)
            added.append(path.name)
    return added


def _shape(answers: list[dict]) -> str:
    import toolset
    actions = toolset.to_actions(answers)
    return "none" if not actions else actions[0]["name"] if len(actions) == 1 else "compound"


def cap_share(rows: list[dict], share: float, seed: int) -> tuple[list[dict], dict]:
    """Subsample any single action above `share` of the examples."""
    import collections
    import random
    groups = collections.defaultdict(list)
    for row in rows:
        groups[_shape(row["answers"])].append(row)
    cap = int(share * len(rows))
    rng = random.Random(seed)
    kept, capped = [], {}
    for name in sorted(groups):
        members = groups[name]
        if len(members) > cap:
            capped[name] = [len(members), cap]
            members = rng.sample(members, cap)
        kept += members
    rng.shuffle(kept)
    return kept, capped


def build_data(library: Path, manifest: dict, out: Path) -> dict:
    """Generate, check against the running tool's rules, write upstream's format."""
    data = manifest["data"]
    added = []
    if data.get("supplements"):
        staged = out.parent / "library"
        added = stage_library(library, data["supplements"], staged)
        library = staged
    corpus = load_generator(library)
    from actions import Action, interpret
    import toolset
    if data["toolset"] != "five":
        raise TuneError("only the five-tool schema is trained")
    exclude = set()
    for rel in data["exclude"]:
        with open(REPO / rel, encoding="utf-8") as handle:
            exclude |= {corpus._fold(json.loads(line)["request"]) for line in handle if line.strip()}
    rows, dropped = corpus.generate(data["seed"], data["per_template"], exclude)
    examples = []
    inconsistent = unsupported = 0
    for row in rows:
        row["actions"] = [[kind, _typed(kind, args)] for kind, args in row["actions"]]
        calls = [{"name": kind, "arguments": args} for kind, args in row["actions"]]
        admitted = [[a.kind, a.args] for a in interpret(row["query"], calls)
                    if isinstance(a, Action)]
        if len(admitted) != len(row["actions"]):
            inconsistent += 1   # a training answer the tool would refuse teaches nothing
            continue
        # The Needle 2 compatibility recipe still has only ten actions.
        # New kilix-ml-only templates must not crash or silently mislabel it.
        try:
            answers = toolset.from_actions(row["actions"])
        except ValueError:
            unsupported += 1
            continue
        examples.append({"query": row["query"], "tools": toolset.TOOLS,
                         "reasoning": reasoning_for(answers, row.get("spans", [])),
                         "answers": answers})
    stats = {"generated": len(rows), "kept": len(examples), "eval_matches_dropped": dropped,
             "inconsistent_dropped": inconsistent, "unsupported_dropped": unsupported,
             "supplement_files": added}
    if data.get("cap_share"):
        examples, stats["capped"] = cap_share(examples, data["cap_share"], data["seed"])
        stats["kept"] = len(examples)
    with open(out, "w", encoding="utf-8") as handle:
        for example in examples:
            handle.write(json.dumps(example, ensure_ascii=False) + "\n")
    return stats


def _offline_prefix() -> list[str]:
    """Run inside a fresh network namespace when the kernel allows it."""
    unshare = shutil.which("unshare")
    if unshare and subprocess.run([unshare, "-rn", "true"], capture_output=True).returncode == 0:
        return [unshare, "-rn"]
    return []


# With qat, the forward pass (and the validation loss) runs on the weights
# export will produce: merge_lora is followed by the checkpoint's own CQ
# quantiser, and gradients pass straight through the rounding. finetune_local
# looks merge_lora up at call time; export runs in its own process, unpatched,
# because export quantises by itself.
_TRAIN = """
import argparse, sys, time, resource
sys.path.insert(0, ".")
from needle.model import finetune
from needle.model.finetune import finetune_local
if {qat}:
    from needle.model.quantize import cq_ste_mixed_params, parse_bits_map
    from needle.model.run import load_checkpoint
    _, config = load_checkpoint("checkpoints/needle2.pkl")
    spec = getattr(config, "weight_bits", "") or ""
    if not spec:
        raise SystemExit("qat: the checkpoint names no weight_bits")
    bits_map, default_bits = parse_bits_map(spec)
    plain_merge = finetune.merge_lora
    finetune.merge_lora = lambda params, lora, scale: cq_ste_mixed_params(
        plain_merge(params, lora, scale), bits_map, default_bits)
    print(f"qat: weight_bits {{spec}}", flush=True)
t = time.time()
finetune_local(argparse.Namespace(jsonl_path={data!r}, checkpoint="checkpoints/needle2.pkl",
    epochs={epochs}, batch_size={batch_size}, lr={learning_rate}, lora_rank={lora_rank},
    lora_alpha={lora_alpha}, max_len={max_len}, val_split={val_split},
    generate=0, model=None, workers=1, checkpoint_dir={out_dir!r}, out={lora!r}))
print(f"TRAIN {{time.time()-t:.0f}}s peak {{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss//1024}} MB")
"""
_EXPORT = """
import argparse, sys
sys.path.insert(0, ".")
from needle.model.finetune import build_main
build_main(argparse.Namespace(checkpoint="checkpoints/needle2.pkl", lora={lora!r}, out={out!r},
                              bits=None, upload=False))
"""


def _checkpoint_unchanged(run: Run, manifest: dict) -> None:
    """The pickle is verified right before each stage that loads it: a resumed
    run otherwise trusted a check made at `base` (review KN-10)."""
    _verify(run.root / "src/checkpoints/needle2.pkl", manifest["base"]["checkpoint_sha256"],
            "checkpoints/needle2.pkl")


def _python_stage(run: Run, script: str, what: str) -> None:
    # generate=0 above keeps upstream from sending anything to its data service;
    # the namespace (when available) makes any network use impossible anyway.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(run.root), "LANG": "C.UTF-8",
           "HF_HUB_OFFLINE": "1", "NEEDLE_TELEMETRY": "0", "DO_NOT_TRACK": "1"}
    offline = _offline_prefix()
    if not offline:
        # Review KN-16: say so, rather than claim a namespace that was not made.
        run.log(f"{what}: unshare -rn is unavailable here, so this stage has network access")
    argv = [*offline, "nice", "-n", "19", str(run.root / "env/bin/python"), "-c", script]
    with open(run.log_path, "a", encoding="utf-8") as log:
        done = subprocess.run(argv, cwd=run.root / "src", env=env, stdout=log, stderr=log)
    if done.returncode != 0:
        raise TuneError(f"{what} failed (status {done.returncode}); see {run.log_path}")


def stage_train(run: Run, manifest: dict) -> None:
    _checkpoint_unchanged(run, manifest)
    train = manifest["train"]
    _python_stage(run, _TRAIN.format(data=str(run.root / "train.jsonl"), out_dir=str(run.root),
                                     lora=str(run.root / "lora.pkl"), **train), "training")
    run.log("train: LoRA adapter written")


def stage_export(run: Run, manifest: dict) -> str:
    _checkpoint_unchanged(run, manifest)
    _python_stage(run, _EXPORT.format(lora=str(run.root / "lora.pkl"),
                                      out=str(run.root / "tuned.cact")), "export")
    digest = sha256_file(run.root / "tuned.cact")
    run.log(f"export: tuned.cact {digest[:12]}")
    return digest


def gate(manifest: dict, results: dict, reference: dict) -> list[str]:
    """Every gate the tuned results fail; empty means select it."""
    gates, failures = manifest["gates"], []
    for name, result in results.items():
        unsafe = result["totals"].get("unsafe", 0)
        if unsafe > gates["unsafe_max"]:
            failures.append(f"{name}: {unsafe} unsafe action(s) admitted")
    held = results["heldout"]
    cases = held["totals"]["cases"]
    gain = 100 * (held["totals"].get("exact", 0) - reference["totals"].get("exact", 0)) / cases
    if gain < gates["min_heldout_exact_gain"]:
        failures.append(f"held-out exact gain {gain:+.1f} points, needs "
                        f"{gates['min_heldout_exact_gain']:+}")
    for tag, row in reference["tags"].items():
        lost = row.get("exact", 0) - held["tags"].get(tag, {}).get("exact", 0)
        if lost > gates["no_tag_regression_above"]:
            failures.append(f"held-out tag {tag} lost {lost} cases")
    return failures


def stage_gates(run: Run, manifest: dict, library_image, cact_sha: str) -> dict:
    import asset
    from evaluate import score
    from libengine import LibEngine
    import toolset
    weights = asset.load_verified(run.root / "tuned.cact", cact_sha,
                                  (run.root / "tuned.cact").stat().st_size)
    sets = {"dev": "evals/dev.jsonl", "test": "evals/test.jsonl",
            "heldout": manifest["gates"]["heldout"]}
    results = {}
    with weights, LibEngine(library_image, toolset.TOOLS, weights) as engine:
        for name, rel in sets.items():
            with open(REPO / rel, encoding="utf-8") as handle:
                cases = [json.loads(line) for line in handle if line.strip()]
            results[name] = score(engine, cases, 1, toolset.to_actions)
    # The reference is measured now, with these checks and this held-out set:
    # the untuned model (the library's built-in weights) with the ten-tool
    # schema, which is the configuration used when no tuned model is selected.
    from actions import LEGACY_TOOLS as TEN
    with open(REPO / sets["heldout"], encoding="utf-8") as handle:
        heldout_cases = [json.loads(line) for line in handle if line.strip()]
    with LibEngine(library_image, TEN) as base:
        reference = score(base, heldout_cases, 1)
    failures = gate(manifest, results, reference)
    report = {"cact_sha256": cact_sha, "failures": failures,
              "totals": {name: r["totals"] for name, r in results.items()},
              "reference_totals": reference["totals"],
              "tags": {"tuned": results["heldout"]["tags"], "reference": reference["tags"]}}
    (run.root / "gates.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    run.log("gates: " + ("PASS" if not failures else "FAIL: " + "; ".join(failures)))
    return report


def select(run_root: Path, cact_sha: str) -> None:
    APP_HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = SELECTION.with_suffix(".tmp")
    tmp.write_text(json.dumps({"weights": str(run_root / "tuned.cact"), "sha256": cact_sha,
                               "toolset": "five", "run": run_root.name}), encoding="utf-8")
    os.replace(tmp, SELECTION)


def select_run(source: Path) -> Path:
    """Select a run tuned elsewhere (e.g. on a rented GPU) once it passes the gates here.

    The run directory must hold tuned.cact and a gates.json with no failures
    for exactly those bytes; both are copied into this installation's tuning
    directory. The report only screens: the gates are then run again here,
    on the copied bytes, and only a pass selects (review KN-06: a hand-written
    report beside random bytes was enough).
    """
    try:
        report = json.loads((source / "gates.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TuneError(f"no readable gate report in {source}: {error}") from error
    if report.get("failures") != []:
        raise TuneError(f"{source.name} did not pass its gates: {report.get('failures')}")
    weights = source / "tuned.cact"
    digest = sha256_file(weights) if weights.is_file() else None
    if digest is None or digest != report.get("cact_sha256"):
        raise TuneError(f"{source.name}: tuned.cact is not the model its gate report scored")
    target = APP_HOME / "tuning" / source.name
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("tuned.cact", "gates.json"):
        shutil.copyfile(source / name, target / name)
    _verify(target / "tuned.cact", digest, "the copied tuned.cact")
    import asset
    from libengine import LibEngineError
    try:
        try:
            library_image = asset.installed_library()
        except asset.AssetError as error:
            raise TuneError(f"the gates need the needle2 runtime to run: {error}") from error
        with library_image:
            try:
                report = stage_gates(Run(target), recipe(load_manifest()), library_image, digest)
            except LibEngineError as error:
                # Review KN-R2-07: rejected bytes were a traceback, not a refusal.
                raise TuneError(f"{source.name}: the runtime rejected the weights ({error})") \
                    from error
        if report["failures"]:
            raise TuneError(f"{source.name} failed the gates here: "
                            f"{'; '.join(report['failures'])}")
    except TuneError:
        shutil.rmtree(target, ignore_errors=True)   # a refused run is not left listed
        raise
    select(target, digest)
    return target


def in_use() -> str:
    """What answers requests now: whatever starts the way a request starts it.

    Review KN-R2-06: checking digests said "tuned" while the runtime refused
    the weights, and "base" when nothing was installed at all.
    """
    import asset
    from libengine import LibEngine, LibEngineError
    import toolset
    choice = selected()
    if choice is not None:
        try:
            development = os.environ.get("KILIX_NEEDLE_LIBRARY")
            with (asset.library_from_file(development) if development
                  else asset.installed_library()) as library, \
                    asset.load_verified(choice["weights"], choice["sha256"],
                                        os.path.getsize(choice["weights"])) as weights, \
                    LibEngine(library, toolset.TOOLS, weights):
                pass
            return f"tuned {choice.get('run', '')}".strip()
        except LibEngineError as error:
            # as a request does: rejected weights are refused, never replaced
            return f"none (the runtime rejected the selected weights: {error})"
        except (asset.AssetError, OSError, KeyError) as error:
            fallback = f" (the selected tuned model is unavailable: {error})"
        else:
            fallback = ""
    else:
        fallback = ""
    try:
        with asset.from_installed():
            pass
    except asset.AssetError as error:
        return f"none ({error})"
    return "base" + fallback


def selected() -> dict | None:
    try:
        return json.loads(SELECTION.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def tune(base_dir: Path | None, library_file: str | None, run_name: str | None) -> int:
    import asset
    manifest = recipe(load_manifest())
    run = Run(APP_HOME / "tuning" / (run_name or
                                     datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    print(f"kilix-needle tune: {run.root}")
    library_image = asset.library_from_file(library_file) if library_file \
        else asset.installed_library()
    with library_image:
        if not run.done("base"):
            stage_base(run, manifest, base_dir); run.mark("base")
        if not run.done("source"):
            stage_source(run, manifest); run.mark("source")
        if not run.done("env"):
            stage_env(run, manifest, LIBRARY); run.mark("env")
        if not run.done("data"):
            stats = build_data(LIBRARY, manifest, run.root / "train.jsonl")
            run.log(f"data: {stats}"); run.mark("data", stats)
        if not run.done("train"):
            stage_train(run, manifest); run.mark("train")
        if not run.done("export"):
            digest = stage_export(run, manifest); run.mark("export", {"sha256": digest})
        digest = json.loads((run.root / ".export.done").read_text())["sha256"]
        report = stage_gates(run, manifest, library_image, digest)
        run.mark("gates", report)
    if report["failures"]:
        print("kilix-needle tune: gates failed; the current model stays in use")
        return 1
    select(run.root, digest)
    run.mark("select")
    run.log("select: the tuned model is now used")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kilix-needle tune", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-dir", type=Path,
                        help="development: a directory holding checkpoints/ and tokenizer/, "
                             "held to the manifest digests")
    parser.add_argument("--library", metavar="FILE",
                        help="development: a local copy of the pinned libneedle.so")
    parser.add_argument("--run", help="resume or name a run")
    parser.add_argument("--background", action="store_true",
                        help="detach and log to the run directory")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--select", metavar="RUN_DIR", type=Path,
                        help="use a run tuned elsewhere whose gate report passed")
    parser.add_argument("--deselect", action="store_true",
                        help="go back to the base model")
    args = parser.parse_args(argv)
    if args.status:
        # "in_use" is what answers; "selected" may be unavailable (review KN-07).
        print(json.dumps({"in_use": in_use(), "selected": selected(), "runs": sorted(
            p.name for p in (APP_HOME / "tuning").glob("*") if p.is_dir())}, indent=1))
        return 0
    if args.select:
        try:
            target = select_run(args.select.expanduser().resolve())
        except TuneError as error:
            print(f"kilix-needle tune: {error}", file=sys.stderr)
            return 1
        print(f"kilix-needle: the tuned model {target.name} is now used")
        return 0
    if args.deselect:
        SELECTION.unlink(missing_ok=True)
        print("kilix-needle: the base model is used")
        return 0
    if args.background:
        name = args.run or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        root = APP_HOME / "tuning" / name
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        rest = [a for a in argv if a != "--background"]
        if not args.run:
            rest += ["--run", name]
        with open(root / "background.log", "a") as log:
            subprocess.Popen([sys.executable, "-B", str(REPO / "needle_cli.py"), "tune", *rest],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)
        print(f"kilix-needle: tuning in the background; progress in {root}/tune.log")
        return 0
    try:
        return tune(args.base_dir, args.library, args.run)
    except TuneError as error:
        print(f"kilix-needle tune: {error}", file=sys.stderr)
        return 1
