#!/usr/bin/env python3
"""Count accompaniment MIDI lengths on a uniform 4/4 bar grid.

One bar is always defined as four quarter-note beats (4 * ticks_per_beat),
regardless of the MIDI time-signature metadata. Source MIDI files are read only.
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


def read_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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


def inspect_midi(path: Path) -> dict[str, Any]:
    midi = mido.MidiFile(path)
    max_note_tick = 0
    max_event_tick = 0
    tempos: list[float] = []
    signatures: list[str] = []
    note_on_count = 0
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
                tempos.append(round(mido.tempo2bpm(message.tempo), 3))
            elif message.type == "time_signature":
                signatures.append(f"{message.numerator}/{message.denominator}")
    ticks_per_4_4_bar = midi.ticks_per_beat * 4
    note_end_bars = max_note_tick / ticks_per_4_4_bar if ticks_per_4_4_bar else 0.0
    timeline_bars = max_event_tick / ticks_per_4_4_bar if ticks_per_4_4_bar else 0.0
    # A note ending inside bar N means that bar is part of the generated music.
    covered_bars = math.ceil(note_end_bars - 1e-9) if note_end_bars > 0 else 0
    return {
        "ticks_per_beat": midi.ticks_per_beat,
        "note_on_count": note_on_count,
        "source_tempos_bpm": "|".join(map(str, sorted(set(tempos)))) if tempos else "120.0(default)",
        "source_time_signatures": "|".join(sorted(set(signatures))) if signatures else "none",
        "note_end_bars_4_4": round(note_end_bars, 4),
        "covered_bars_4_4": covered_bars,
        "timeline_bars_4_4": round(timeline_bars, 4),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: list[dict[str, Any]], sampled_ids: set[str]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    methods = list(dict.fromkeys(row["method"] for row in rows))
    for method in methods:
        method_rows = [row for row in rows if row["method"] == method]
        all_values = [int(row["covered_bars_4_4"]) for row in method_rows]
        sampled_values = [int(row["covered_bars_4_4"]) for row in method_rows if row["sample_id"] in sampled_ids]
        distribution = Counter(all_values)
        summary.append({
            "method": method,
            "file_count": len(method_rows),
            "min_bars_4_4": min(all_values),
            "median_bars_4_4": round(statistics.median(all_values), 2),
            "mean_bars_4_4": round(statistics.fmean(all_values), 2),
            "max_bars_4_4": max(all_values),
            "bar_count_distribution": "; ".join(f"{bars}:{count}" for bars, count in sorted(distribution.items())),
            "sampled_file_count": len(sampled_values),
            "sampled_min_bars_4_4": min(sampled_values) if sampled_values else "",
            "sampled_median_bars_4_4": round(statistics.median(sampled_values), 2) if sampled_values else "",
            "sampled_mean_bars_4_4": round(statistics.fmean(sampled_values), 2) if sampled_values else "",
            "sampled_max_bars_4_4": max(sampled_values) if sampled_values else "",
        })
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="evaluation.config.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary-output", type=Path)
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = read_config(config_path)
    task = next(
        item for item in config["tasks"]
        if item["task_type"] == "accompaniment" or item.get("task_kind") == "accompaniment"
    )
    workspace = resolve(config["evaluation_workspace"], config_path.parent)
    sampled_sets_path = workspace / "manifests" / "sampled_sets.json"
    sampled_ids: set[str] = set()
    if sampled_sets_path.is_file():
        sample_sets = json.loads(sampled_sets_path.read_text(encoding="utf-8"))["sample_sets"]
        sampled_ids = set(sample_sets[task.get("sample_set", task["task_type"])]["sample_ids"])

    rows: list[dict[str, Any]] = []
    for method in task["methods"]:
        directory = resolve(method["midi_dir"], config_path.parent)
        for sample_id, path in midi_index(directory).items():
            rows.append({
                "method": method["name"],
                "sample_id": sample_id,
                "is_currently_sampled": sample_id in sampled_ids,
                "source_path": str(path),
                **inspect_midi(path),
            })
    if not rows:
        raise ValueError("No accompaniment MIDI files found")

    detail_path = args.output or workspace / "reports" / "accompaniment_bar_counts_4_4.csv"
    summary_path = args.summary_output or detail_path.with_name(detail_path.stem + "_summary.csv")
    write_csv(detail_path, rows)
    summary = summarize(rows, sampled_ids)
    write_csv(summary_path, summary)
    print(json.dumps({
        "bar_definition": "1 bar = 4 quarter-note beats = 4 * ticks_per_beat",
        "detail": str(detail_path),
        "summary": str(summary_path),
        "models": summary,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
