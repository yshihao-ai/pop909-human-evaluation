#!/usr/bin/env python3
"""Audit continuation timing and prompt identity without modifying source MIDI."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import mido


SCALES = (0.25, 0.5, 1.0, 2.0, 4.0)


def midi_profile(path: Path) -> dict:
    midi = mido.MidiFile(path)
    numerator, denominator = 4, 4
    tempos: list[float] = []
    programs: set[int] = set()
    events: list[tuple[float, int]] = []
    max_tick = 0
    for track in midi.tracks:
        absolute = 0
        for message in track:
            absolute += message.time
            if message.type == "time_signature" and absolute == 0:
                numerator, denominator = message.numerator, message.denominator
            elif message.type == "set_tempo":
                tempos.append(round(mido.tempo2bpm(message.tempo), 3))
            elif message.type == "program_change":
                programs.add(message.program)
            elif message.type == "note_on" and message.velocity > 0:
                events.append((absolute / midi.ticks_per_beat, message.note))
                max_tick = max(max_tick, absolute)
            elif message.type == "note_off" or (message.type == "note_on" and message.velocity == 0):
                max_tick = max(max_tick, absolute)
    beats_per_bar = numerator * 4 / denominator
    return {
        "ticks_per_beat": midi.ticks_per_beat,
        "signature": f"{numerator}/{denominator}",
        "beats_per_bar": beats_per_bar,
        "meter_scale": 4 / beats_per_bar,
        "tempos": tempos or [120.0],
        "programs": sorted(programs),
        "events": events,
        "max_beat": max_tick / midi.ticks_per_beat,
    }


def prompt_set(profile: dict, scale: float, prompt_beats: float = 16.0) -> set[tuple[int, int]]:
    return {
        (round(beat * scale * 24), pitch)
        for beat, pitch in profile["events"]
        if beat * scale < prompt_beats
    }


def f1(reference: set[tuple[int, int]], candidate: set[tuple[int, int]]) -> float:
    if not reference and not candidate:
        return 1.0
    if not reference or not candidate:
        return 0.0
    overlap = len(reference & candidate)
    precision = overlap / len(candidate)
    recall = overlap / len(reference)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    workspace = Path(config["evaluation_workspace"])
    sample_ids = json.loads((workspace / "manifests" / "sampled_ids.json").read_text(encoding="utf-8"))["sample_ids"]
    task = next(item for item in config["tasks"] if item["task_type"] == "continuation")
    methods = {item["name"]: Path(item["midi_dir"]) for item in task["methods"]}
    reference_name = "GroundTruth" if "GroundTruth" in methods else "DID"
    rows: list[dict] = []
    for sample_id in sample_ids:
        reference = midi_profile(methods[reference_name] / f"{sample_id}.mid")
        reference_prompt = prompt_set(reference, 1.0)
        for method, directory in methods.items():
            profile = midi_profile(directory / f"{sample_id}.mid")
            # Compare from the first musical onset, tolerating a constant leading
            # phase shift; evaluate short and full prefixes independently.
            reference_start = min(t for t, _ in reference['events'])
            candidate_start = min(t for t, _ in profile['events'])
            def aligned_score(scale: float, beats: float) -> float:
                ref = {(round((t-reference_start)*8), p) for t,p in reference['events'] if 0 <= t-reference_start < beats}
                cand = {(round((t-candidate_start)*scale*8), p) for t,p in profile['events'] if 0 <= (t-candidate_start)*scale < beats}
                return f1(ref, cand)
            scores = {scale: aligned_score(scale, 4) for scale in SCALES}
            best_scale = max(SCALES, key=lambda scale: (scores[scale], -abs(scale-1)))
            def shifted_score(shift: float, end: float) -> float:
                ref = {(round(t*8),p) for t,p in reference['events'] if 0 <= t < end}
                cand = {(round((t+shift)*8),p) for t,p in profile['events'] if 0 <= t+shift < end}
                return f1(ref,cand)
            shifts = [i/8 for i in range(-128,129)]
            phase = max(shifts, key=lambda s: (shifted_score(s,16), -abs(s)))
            meter_scale = profile["meter_scale"]
            rows.append({
                "sample_id": sample_id,
                "method": method,
                "source_time_signature": profile["signature"],
                "source_tempos_bpm": "|".join(map(str, profile["tempos"])),
                "programs": "|".join(map(str, profile["programs"])),
                "raw_total_beats": round(profile["max_beat"], 4),
                "meter_scale": round(meter_scale, 4),
                "best_scale": best_scale,
                "raw_prompt_f1": round(f1(reference_prompt, prompt_set(profile, 1.0)), 4),
                "meter_prompt_f1": round(f1(reference_prompt, prompt_set(profile, meter_scale)), 4),
                "best_prompt_f1": round(scores[best_scale], 4),
                "best_total_bars_4_4": round(profile["max_beat"] * best_scale / 4, 4),
                "reference_prompt_events": len(reference_prompt),
                "candidate_prompt_events": len(prompt_set(profile, best_scale)),
                "first_onset_offset_beats": round(candidate_start-reference_start, 4),
                "aligned_1bar_f1_scale1": round(aligned_score(1,4),4),
                "aligned_4bar_f1_scale1": round(aligned_score(1,16),4),
                "actual_bars_at_scale1": round(profile['max_beat']/4,4),
                "best_phase_shift_beats": phase,
                "phase_aligned_gt_4bar_f1": round(shifted_score(phase,16),4),
            })
    report_path = args.output or workspace / "reports" / "continuation_timing_prompt_audit.csv"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(report_path)


if __name__ == "__main__":
    main()
