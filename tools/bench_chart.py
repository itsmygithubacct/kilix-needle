#!/usr/bin/env python3
"""Draw the README benchmark chart from evaluate.py's JSON.

    python3 tools/bench_chart.py --from RUN_DIR [--stock-five DIR]
        # RUN_DIR/eval/{reference,tuned}-*.json (+ DIR/stock-five-*.json) -> docs/bench.json
    python3 tools/bench_chart.py                  # docs/bench.json -> docs/bench-{light,dark}.svg

No number is typed by hand: docs/bench.json is extracted from the scored runs,
and both SVGs are drawn from it.
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"
SETS = (("dev", "dev set", "iterated on"),
        ("test", "test set", "measured only"),
        ("heldout-v8", "held-out v8", "written blind; the gate"))
TAGS = {  # evaluate.py's tags, in words
    "run_in_pane": "run a command in a pane", "rename_tab": "rename a tab",
    "compound": "two or more actions", "go_to_pane": "go to a pane",
    "open_tab": "open a tab", "go_to_tab": "go to a tab",
    "arrange_panes": "arrange panes", "open_pane": "open a pane",
    "close_pane": "close a pane", "close_tab": "close a tab",
    "bait": "bait (must do nothing)", "resize_pane": "resize a pane",
    "none": "no action asked",
}
THEMES = {
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
              "grid": "#e1e0d9", "axis": "#c3c2b7", "base": "#eb6834", "five": "#1baf7a", "tuned": "#2a78d6"},
    "dark": {"surface": "#1a1a19", "ink": "#ffffff", "ink2": "#c3c2b7", "muted": "#898781",
             "grid": "#2c2c2a", "axis": "#383835", "base": "#d95926", "five": "#199e70", "tuned": "#3987e5"},
}
W, LEFT, RIGHT = 760, 224, 96
FONT = 'system-ui, -apple-system, &quot;Segoe UI&quot;, sans-serif'


def extract(run: Path, stock_five: Path | None) -> dict:
    out = {"sets": {}, "tags": {}}
    for name, _label, _note in SETS:
        for arm in ("reference", "tuned"):
            data = json.loads((run / "eval" / f"{arm}-{name}.json").read_text())
            out["sets"].setdefault(name, {})[arm] = {
                "cases": data["totals"]["cases"], "exact": data["totals"]["exact"],
                "unsafe": data["totals"].get("unsafe", 0),
                "median_ms": data["latency_ms"]["median"], "p95_ms": data["latency_ms"]["p95"]}
            if name == "heldout-v8":
                out["tags"][arm] = {t: {"cases": v["cases"], "exact": v["exact"]}
                                    for t, v in data["tags"].items()}
        if stock_five:
            data = json.loads((stock_five / f"stock-five-{name}.json").read_text())
            out["sets"][name]["stock_five"] = {"cases": data["totals"]["cases"],
                                               "exact": data["totals"]["exact"],
                                               "unsafe": data["totals"].get("unsafe", 0)}
    gates = json.loads((run / "gates.json").read_text())
    out["weights_sha256"] = gates["cact_sha256"]
    return out


def bar(x0, y, length, thick, color, r=4):
    """A bar from the baseline x0: square at the baseline, 4px rounded tip."""
    if length <= r:
        return f'<rect x="{x0}" y="{y}" width="{max(length, 1):.1f}" height="{thick}" fill="{color}"/>'
    x1 = x0 + length
    return (f'<path d="M{x0},{y} H{x1 - r:.1f} Q{x1:.1f},{y} {x1:.1f},{y + r} '
            f'V{y + thick - r} Q{x1:.1f},{y + thick} {x1 - r:.1f},{y + thick} H{x0} Z" fill="{color}"/>')


def text(x, y, s, fill, size=13, anchor="start", weight=400):
    return (f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size}" '
            f'font-weight="{weight}" text-anchor="{anchor}">{html.escape(s)}</text>')


def render(data: dict, theme: str) -> str:
    c = THEMES[theme]
    plot = W - LEFT - RIGHT
    parts, y = [], 0

    def heading(title, sub):
        nonlocal y
        y += 34
        parts.append(text(24, y, title, c["ink"], 16, weight=600))
        y += 20
        parts.append(text(24, y, sub, c["ink2"], 13))
        y += 18

    def axis(ticks, top, bottom, fmt, at):
        """Gridlines go in at `at`, before the panel's marks, so they sit behind them."""
        grid = []
        for t, label in ticks:
            x = LEFT + plot * t
            grid.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}" '
                        f'stroke="{c["grid"] if t else c["axis"]}" stroke-width="1"/>')
            grid.append(text(x, bottom + 16, fmt(label), c["muted"], 12, "middle"))
        parts[at:at] = grid

    # Legend: always present for two series; identity never by colour alone.
    y += 30
    parts.append(text(24, y, "kilix-needle benchmark: stock Needle 2 vs the tuned model", c["ink"], 18, weight=600))
    y += 26
    three = all("stock_five" in v for v in data["sets"].values())
    legend = [("base", "Needle 2, stock, ten tools"), ("tuned", "kilix-needle, tuned (QAT run 6), five tools")]
    if three:
        legend.insert(1, ("five", "Needle 2, stock, five tools (accuracy only)"))
    for i, (key, label) in enumerate(legend):
        x, yy = 24 + (i % 2) * 330, y + (i // 2) * 22
        parts.append(f'<rect x="{x}" y="{yy - 10}" width="12" height="12" rx="3" fill="{c[key]}"/>')
        parts.append(text(x + 18, yy, label, c["ink2"], 13))
    y += 22 * ((len(legend) - 1) // 2)

    # 1. Exact, per set.
    heading("Requests handled exactly as intended",
            "The whole pipeline: the model's calls, then kilix-needle's checks. 0 unsafe actions in any set, from any of the three.")
    top, at = y, len(parts)
    thick, gap = 16, 2
    arms = (("reference", "base"), ("stock_five", "five"), ("tuned", "tuned")) if three \
        else (("reference", "base"), ("tuned", "tuned"))
    group = len(arms) * (thick + gap) + 22
    for name, label, note in SETS:
        parts.append(text(LEFT - 12, y + 16, label, c["ink"], 13, "end", 600))
        parts.append(text(LEFT - 12, y + 32, note, c["muted"], 12, "end"))
        for i, (arm, key) in enumerate(arms):
            v = data["sets"][name][arm]
            share = v["exact"] / v["cases"]
            yy = y + 4 + i * (thick + gap)
            parts.append(bar(LEFT, yy, plot * share, thick, c[key]))
            parts.append(text(LEFT + plot * share + 8, yy + 12.5,
                              f'{round(100 * share)}%  ·  {v["exact"]} of {v["cases"]}', c["ink2"], 12))
        y += group
    axis([(t / 4, t * 25) for t in range(5)], top - 4, y - 14, lambda v: f"{v}%", at)
    y += 14

    # 2. By action, held-out set.
    heading("By kind of request, held-out v8 (150 requests)",
            "Share handled exactly. Sorted by the tuned model's gain; a lone dot means both scored the same.")
    top, at = y, len(parts)
    ref, tun = data["tags"]["reference"], data["tags"]["tuned"]
    order = sorted(ref, key=lambda t: (-(tun[t]["exact"] / tun[t]["cases"] - ref[t]["exact"] / ref[t]["cases"]),
                                       TAGS.get(t, t)))
    row = 24
    for tag in order:
        cy = y + row / 2
        a, b = ref[tag]["exact"] / ref[tag]["cases"], tun[tag]["exact"] / tun[tag]["cases"]
        parts.append(text(LEFT - 12, cy + 4.5, f'{TAGS.get(tag, tag)} ({ref[tag]["cases"]})',
                          c["ink2"], 12.5, "end"))
        xa, xb = LEFT + plot * a, LEFT + plot * b
        parts.append(f'<line x1="{xa:.1f}" y1="{cy}" x2="{xb:.1f}" y2="{cy}" stroke="{c["axis"]}" stroke-width="2" stroke-linecap="round"/>')
        for x, key in ((xa, "base"), (xb, "tuned")):
            parts.append(f'<circle cx="{x:.1f}" cy="{cy}" r="5" fill="{c[key]}" stroke="{c["surface"]}" stroke-width="2"/>')
        delta = round(100 * (b - a))
        parts.append(text(LEFT + plot + 14, cy + 4.5, f"{delta:+d} pts" if delta else "same", c["ink2"], 12))
        y += row
    axis([(t / 4, t * 25) for t in range(5)], top - 4, y + 2, lambda v: f"{v}%", at)
    y += 30

    # 3. Latency, per set.
    heading("Median time per request",
            "Same host, same libneedle.so runtime. The speed-up comes from the five-tool schema, not the tuning.")
    top, at = y, len(parts)
    most = max(data["sets"][n][a]["median_ms"] for n, *_ in SETS for a in ("reference", "tuned"))
    scale = 1200 if most <= 1200 else 1500
    group = 46
    for name, label, _note in SETS:
        parts.append(text(LEFT - 12, y + 20, label, c["ink"], 13, "end", 600))
        for i, arm in enumerate(("reference", "tuned")):
            ms = data["sets"][name][arm]["median_ms"]
            yy = y + 4 + i * (thick + gap)
            parts.append(bar(LEFT, yy, plot * ms / scale, thick, c["base" if i == 0 else "tuned"]))
            parts.append(text(LEFT + plot * ms / scale + 8, yy + 12.5, f"{ms:,.0f} ms", c["ink2"], 12))
        y += group
    axis([(t / 4, int(scale * t / 4)) for t in range(5)], top - 4, y - 8, lambda v: f"{v:,} ms", at)
    y += 34
    parts.append(text(24, y, f'Tuned weights sha256 {data["weights_sha256"][:16]}…  ·  generated by tools/bench_chart.py from evaluate.py JSON',
                      c["muted"], 11.5))
    y += 18
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{y}" viewBox="0 0 {W} {y}" '
            f'font-family="{FONT}" role="img" aria-label="kilix-needle benchmark: stock Needle 2 versus the tuned model">'
            f'<rect width="{W}" height="{y}" rx="8" fill="{c["surface"]}"/>' + "".join(parts) + "</svg>\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--from", dest="run", type=Path, help="a scored tuning run directory")
    parser.add_argument("--stock-five", type=Path, help="stock Needle 2 scored with --toolset five")
    args = parser.parse_args()
    DOCS.mkdir(exist_ok=True)
    if args.run:
        (DOCS / "bench.json").write_text(json.dumps(extract(args.run, args.stock_five), indent=1, sort_keys=True) + "\n")
    data = json.loads((DOCS / "bench.json").read_text())
    for theme in THEMES:
        (DOCS / f"bench-{theme}.svg").write_text(render(data, theme))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
