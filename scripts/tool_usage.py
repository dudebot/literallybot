#!/usr/bin/env python3
"""Summarize agent tool-usage telemetry (logs/agent_tools/*.jsonl).

    venv/bin/python scripts/tool_usage.py [--days 7] [--guild ID] [--model NAME] [--dir PATH]

Prints per-model requests (model round trips), tokens and cost per run,
per-tool counts and failure rates, calls-per-run, an hour-of-day
histogram, call transitions (START/END included, i.e. a first-order Markov
view) and same-tool repeats within one run. Repeated chains and hot
transitions are the candidates for a bundled tool or a prompt fix.
"""
import argparse
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.tool_telemetry import TOOL_LOG_DIR, iter_runs  # noqa: E402

BAR = 40


def bars(counter, keys, label_width):
    peak = max(counter.values(), default=0) or 1
    for key in keys:
        n = counter.get(key, 0)
        print(f"  {str(key):>{label_width}} {'#' * round(BAR * n / peak):<{BAR}} {n}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--days', type=int, default=7, help='window ending today (0 = all)')
    parser.add_argument('--guild', type=int, help='only runs in this guild')
    parser.add_argument('--model', help='only runs on this model')
    parser.add_argument('--dir', type=Path, default=TOOL_LOG_DIR)
    args = parser.parse_args()

    since = (date.today() - timedelta(days=args.days - 1)).isoformat() if args.days else None
    runs = [r for r in iter_runs(since, args.dir)
            if (args.guild is None or r.get('guild_id') == args.guild)
            and (args.model is None or r.get('model') == args.model)]
    if not runs:
        print(f"No runs in {args.dir}" + (f" since {since}" if since else ""))
        return

    calls = [c for r in runs for c in r.get('calls', [])]
    print(f"{len(runs)} runs, {len(calls)} tool calls"
          + (f" since {since}" if since else "")
          + f"  ({runs[0]['ts']} .. {runs[-1]['ts']})")
    print("outcomes: " + ", ".join(f"{k}={v}" for k, v in
                                   Counter(r.get('outcome') for r in runs).most_common()))

    # Rows written before these counters existed are left out of the medians.
    print("\nper model                     runs  calls/run  requests/run  prompt_tok  cached%  completion_tok  cost_usd")
    by_model = defaultdict(list)
    for r in runs:
        by_model[r.get('model')].append(r)
    for model, rows in sorted(by_model.items(), key=lambda kv: -len(kv[1])):
        counted = [r for r in rows if r.get('requests')]
        med = lambda key: statistics.median(r[key] for r in counted) if counted else '-'
        prompt = sum(r['prompt_tokens'] for r in counted)
        cached = (f"{100 * sum(r['cached_prompt_tokens'] for r in counted) / prompt:.0f}"
                  if prompt else '-')
        costs = [r.get('est_cost_usd') for r in counted]
        cost = f"{sum(costs):.4f}" if counted and None not in costs else '-'
        print(f"  {str(model)[:28]:<28}{len(rows):>5}"
              f"{statistics.mean(len(r.get('calls', [])) for r in rows):>11.2f}"
              f"{med('requests'):>14}{med('prompt_tokens'):>12}{cached:>9}"
              f"{med('completion_tokens'):>16}{cost:>10}")

    print("\nper tool                      calls   ok  err  refused  median_ms")
    by_tool = defaultdict(list)
    for c in calls:
        by_tool[c['tool']].append(c)
    for tool, items in sorted(by_tool.items(), key=lambda kv: -len(kv[1])):
        status = Counter(c['status'] for c in items)
        refused = sum(v for k, v in status.items() if k.startswith('refused'))
        ms = [c['ms'] for c in items if isinstance(c.get('ms'), (int, float))]
        print(f"  {tool:<28}{len(items):>5}{status['ok']:>5}"
              f"{status['error'] + status['exception']:>5}{refused:>9}"
              f"{statistics.median(ms) if ms else '-':>11}")
    enabled_never_used = (set().union(*(r.get('enabled', []) for r in runs))
                          - set(by_tool))
    if enabled_never_used:
        print("  enabled but never called: " + ", ".join(sorted(enabled_never_used)))

    print("\ncalls per run")
    per_run = Counter(len(r.get('calls', [])) for r in runs)
    bars(per_run, range(max(per_run) + 1), 3)

    print("\ncalls by hour of day")
    by_hour = Counter(int(r['ts'][11:13]) for r in runs for _ in r.get('calls', []))
    bars(by_hour, range(24), 3)

    print("\ntop transitions")
    transitions = Counter()
    for r in runs:
        seq = [c['tool'] for c in r.get('calls', [])]
        if seq:
            transitions.update(zip(['START'] + seq, seq + ['END']))
    for (a, b), n in transitions.most_common(15):
        print(f"  {n:>5}  {a} -> {b}")

    print("\nsame tool 3+ times in one run")
    repeats = Counter()
    for r in runs:
        for tool, n in Counter(c['tool'] for c in r.get('calls', [])).items():
            if n >= 3:
                repeats[tool] += 1
    for tool, n in repeats.most_common() or [("(none)", 0)]:
        print(f"  {n:>5}  {tool}")


if __name__ == '__main__':
    main()
