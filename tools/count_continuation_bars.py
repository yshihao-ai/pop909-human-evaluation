#!/usr/bin/env python3
"""Count continuation MIDI lengths on a uniform 4/4 grid.

One bar is always four quarter-note beats (4 * ticks_per_beat), regardless of
time-signature metadata. Source MIDI directories are read only.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

import mido


MIDI_SUFFIXES = {".mid", ".midi"}


def resolve(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def midi_index(directory: Path) -> dict[str, Path]:
    if not directory.is_dir():
        raise FileNotFoundError(f"MIDI directory does not exist: {directory}")
    result: dict[str, Path] = {}
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_file() and path.suffix.casefold() in MIDI_SUFFIXES:
            if path.stem in result:
                raise ValueError(f"Duplicate sample_id '{path.stem}' in {directory}")
            result[path.stem] = path.resolve()
    return result


def inspect_midi(path: Path, prompt_bars: float) -> dict[str, Any]:
    midi = mido.MidiFile(path)
    max_note_tick = 0
    max_event_tick = 0
    note_on_count = 0
    tempos: set[float] = set()
    signatures: set[str] = set()
    for track in midi.tracks:
        absolute = 0
        for message in track:
            absolute += message.time
            max_event_tick = max(max_event_tick, absolute)
            if message.type in {"note_on", "note_off"}:
                max_note_tick = max(max_note_tick, absolute)
            if message.type == "note_on" and message.velocity > 0:
                note_on_count += 1
            elif message.type == "set_tempo":
                tempos.add(round(mido.tempo2bpm(message.tempo), 3))
            elif message.type == "time_signature":
                signatures.add(f"{message.numerator}/{message.denominator}")
    ticks_per_bar = midi.ticks_per_beat * 4
    note_end_bars = max_note_tick / ticks_per_bar if ticks_per_bar else 0.0
    covered_total_bars = math.ceil(note_end_bars - 1e-9) if note_end_bars > 0 else 0
    exact_continuation_bars = max(0.0, note_end_bars - prompt_bars)
    covered_continuation_bars = max(0, covered_total_bars - math.ceil(prompt_bars))
    return {
        "ticks_per_beat": midi.ticks_per_beat,
        "note_on_count": note_on_count,
        "source_tempos_bpm": "|".join(map(str, sorted(tempos))) if tempos else "120.0(default)",
        "source_time_signatures": "|".join(sorted(signatures)) if signatures else "none",
        "note_end_total_bars_4_4": round(note_end_bars, 4),
        "covered_total_bars_4_4": covered_total_bars,
        "exact_continuation_bars_4_4": round(exact_continuation_bars, 4),
        "covered_continuation_bars_4_4": covered_continuation_bars,
        "timeline_total_bars_4_4": round(max_event_tick / ticks_per_bar, 4) if ticks_per_bar else 0.0,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def stats(values: list[int], prefix: str) -> dict[str, Any]:
    return {
        f"{prefix}_min": min(values),
        f"{prefix}_median": round(statistics.median(values), 2),
        f"{prefix}_mean": round(statistics.fmean(values), 2),
        f"{prefix}_max": max(values),
    }


def summarize(rows: list[dict[str, Any]], sampled_ids: set[str]) -> list[dict[str, Any]]:
    summary = []
    for method in dict.fromkeys(row["method"] for row in rows):
        method_rows = [row for row in rows if row["method"] == method]
        total = [int(row["covered_total_bars_4_4"]) for row in method_rows]
        continuation = [int(row["covered_continuation_bars_4_4"]) for row in method_rows]
        sampled_rows = [row for row in method_rows if row["sample_id"] in sampled_ids]
        sampled_total = [int(row["covered_total_bars_4_4"]) for row in sampled_rows]
        sampled_continuation = [int(row["covered_continuation_bars_4_4"]) for row in sampled_rows]
        distribution = Counter(total)
        item = {
            "method": method,
            "file_count": len(method_rows),
            **stats(total, "total_bars_4_4"),
            **stats(continuation, "continuation_bars_4_4"),
            "total_bar_distribution": "; ".join(f"{bars}:{count}" for bars, count in sorted(distribution.items())),
            "sampled_file_count": len(sampled_rows),
        }
        if sampled_rows:
            item.update(stats(sampled_total, "sampled_total_bars_4_4"))
            item.update(stats(sampled_continuation, "sampled_continuation_bars_4_4"))
        summary.append(item)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="evaluation.config.json")
    parser.add_argument("--task-type", default="continuation_pop1k7")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary-output", type=Path)
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    task = next(item for item in config["tasks"] if item["task_type"] == args.task_type)
    prompt_bars = float(task.get("prompt_length_bars", 4))
    workspace = resolve(config["evaluation_workspace"], config_path.parent)
    sampled_sets = json.loads((workspace / "manifests" / "sampled_sets.json").read_text(encoding="utf-8"))["sample_sets"]
    sampled_ids = set(sampled_sets[task.get("sample_set", task["task_type"])]["sample_ids"])

    rows: list[dict[str, Any]] = []
    for method in task["methods"]:
        directory = resolve(method["midi_dir"], config_path.parent)
        for sample_id, path in midi_index(directory).items():
            rows.append({
                "task_type": task["task_type"],
                "method": method["name"],
                "sample_id": sample_id,
                "is_currently_sampled": sample_id in sampled_ids,
                "prompt_bars_4_4": prompt_bars,
                "source_path": str(path),
                **inspect_midi(path, prompt_bars),
            })
    if not rows:
        raise ValueError("No continuation MIDI files found")

    detail_path = args.output or workspace / "reports" / f"{task['task_type']}_bar_counts_4_4.csv"
    summary_path = args.summary_output or detail_path.with_name(detail_path.stem + "_summary.csv")
    write_csv(detail_path, rows)
    summary = summarize(rows, sampled_ids)
    write_csv(summary_path, summary)
    print(json.dumps({
        "task_type": task["task_type"],
        "bar_definition": "1 bar = 4 quarter-note beats = 4 * ticks_per_beat",
        "prompt_bars_4_4": prompt_bars,
        "detail": str(detail_path),
        "summary": str(summary_path),
        "models": summary,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
