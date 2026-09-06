#!/usr/bin/env python3
"""Reproducible, read-only-safe POP909 evaluation data preparation.

The script never writes inside source MIDI directories. All generated files are
placed below the configured evaluation_workspace directory.
"""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

MIDI_SUFFIXES = {".mid", ".midi"}
SAMPLING_GROUPS_FORMAT = "pop909-task-sampling-groups-v1"
REPORT_FIELDS = [
    "task_type", "sample_id", "method", "source_path", "processed_midi_path",
    "audio_path", "original_bpm", "target_bpm", "prompt_length_bars",
    "continuation_length_bars", "final_length_bars", "original_duration_sec",
    "final_duration_sec", "source_time_signature", "tick_scale_factor",
    "source_bars_4_4", "length_warning", "prompt_policy", "loudness_before",
    "loudness_after", "status", "error_message",
]


def read_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve(path: str | Path, base: Path) -> Path:
    value = Path(path).expanduser()
    return (value if value.is_absolute() else base / value).resolve()


def ensure_isolated(workspace: Path, source_dirs: list[Path]) -> None:
    workspace = workspace.resolve()
    for source in source_dirs:
        source = source.resolve()
        if workspace == source or workspace in source.parents or source in workspace.parents:
            raise ValueError(f"evaluation_workspace must be isolated from source MIDI directories: {source}")
    workspace.mkdir(parents=True, exist_ok=True)


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


def covered_bars_4_4(path: Path) -> int:
    """Return the last 4/4 bar containing a note event."""
    import mido
    midi = mido.MidiFile(path)
    max_note_tick = 0
    for track in midi.tracks:
        absolute = 0
        for message in track:
            absolute += message.time
            if message.type in {"note_on", "note_off"}:
                max_note_tick = max(max_note_tick, absolute)
    bars = max_note_tick / (midi.ticks_per_beat * 4)
    return math.ceil(bars - 1e-9) if bars > 0 else 0


def all_methods(config: dict[str, Any], config_dir: Path) -> list[tuple[str, str, Path]]:
    rows: list[tuple[str, str, Path]] = []
    for task in config["tasks"]:
        for method in task["methods"]:
            rows.append((task["task_type"], method["name"], resolve(method["midi_dir"], config_dir)))
    return rows


def task_sample_set(task: dict[str, Any]) -> str:
    return str(task.get("sample_set", task["task_type"]))


def task_kind(task: dict[str, Any]) -> str:
    return str(task.get("task_kind", task["task_type"]))


def sampling_specs(config: dict[str, Any], config_dir: Path) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for task in config["tasks"]:
        sample_set = task_sample_set(task)
        spec = {
            "reference_dir": resolve(task.get("sample_reference_midi_dir", config["reference_midi_dir"]), config_dir),
            "seed": int(task.get("seed", config.get("seed", 42))),
            "num_samples": int(task.get("num_samples", config.get("num_samples", 10))),
            "minimum_common_covered_bars_4_4": int(task.get("minimum_common_covered_bars_4_4", 0)),
            "eligibility_dirs": tuple(
                resolve(method["midi_dir"], config_dir) for method in task["methods"]
            ) if task.get("minimum_common_covered_bars_4_4") else (),
        }
        previous = specs.get(sample_set)
        if previous and previous != spec:
            raise ValueError(f"Tasks sharing sample_set '{sample_set}' must use the same sampling configuration")
        specs[sample_set] = spec
    return specs


def load_sample_sets(workspace: Path, config: dict[str, Any]) -> dict[str, list[str]]:
    current = workspace / "manifests" / "sampled_sets.json"
    if current.is_file():
        payload = json.loads(current.read_text(encoding="utf-8"))
        return {name: value["sample_ids"] for name, value in payload["sample_sets"].items()}
    legacy = workspace / "manifests" / "sampled_ids.json"
    sampled = json.loads(legacy.read_text(encoding="utf-8"))["sample_ids"]
    return {task_sample_set(task): sampled for task in config["tasks"]}


def task_sampling_groups(workspace: Path, config: dict[str, Any], task: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the original reproducible sample plus administrator-added task groups."""
    sampled_sets = load_sample_sets(workspace, config)
    groups = [{
        "id": "primary",
        "title": "初始抽样",
        "sample_ids": list(sampled_sets[task_sample_set(task)]),
    }]
    path = workspace / "manifests" / "task_sampling_groups.json"
    if not path.is_file():
        return groups
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format") != SAMPLING_GROUPS_FORMAT:
        raise ValueError(f"Unsupported sampling-group format in {path}")
    seen_ids = {"primary"}
    for item in payload.get("tasks", {}).get(task["task_type"], []):
        group_id = str(item.get("id", "")).strip()
        title = str(item.get("title", "")).strip()
        sample_ids = [str(value).strip() for value in item.get("sample_ids", []) if str(value).strip()]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", group_id):
            raise ValueError(f"Invalid sampling-group id for {task['task_type']}: {group_id!r}")
        if group_id in seen_ids:
            raise ValueError(f"Duplicate sampling-group id for {task['task_type']}: {group_id}")
        if not title or not sample_ids:
            raise ValueError(f"Sampling group {task['task_type']}/{group_id} needs a title and sample_ids")
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError(f"Sampling group {task['task_type']}/{group_id} contains duplicate MIDI names")
        seen_ids.add(group_id)
        groups.append({"id": group_id, "title": title, "sample_ids": sample_ids})
    return groups


def task_sample_ids(workspace: Path, config: dict[str, Any], task: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(
        sample_id
        for group in task_sampling_groups(workspace, config, task)
        for sample_id in group["sample_ids"]
    ))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def sample_command(config: dict[str, Any], config_dir: Path) -> None:
    workspace = resolve(config["evaluation_workspace"], config_dir)
    methods = all_methods(config, config_dir)
    specs = sampling_specs(config, config_dir)
    ensure_isolated(workspace, [*(spec["reference_dir"] for spec in specs.values()), *(path for _, _, path in methods)])

    manifest_dir = workspace / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    sampled_sets: dict[str, list[str]] = {}
    payload: dict[str, Any] = {"sample_sets": {}}
    text_rows: list[str] = []
    for sample_set, spec in specs.items():
        ordered_ids = sorted(midi_index(spec["reference_dir"]), key=str.casefold)
        count = spec["num_samples"]
        seed = spec["seed"]
        minimum_bars = spec["minimum_common_covered_bars_4_4"]
        if minimum_bars:
            method_indices = [midi_index(directory) for directory in spec["eligibility_dirs"]]
            ordered_ids = [
                sample_id for sample_id in ordered_ids
                if all(
                    sample_id in index and covered_bars_4_4(index[sample_id]) >= minimum_bars
                    for index in method_indices
                )
            ]
        if len(ordered_ids) < count:
            raise ValueError(
                f"Eligible pool for sample_set '{sample_set}' contains {len(ordered_ids)} MIDI files; "
                f"{count} are required"
            )
        sampled = random.Random(seed).sample(ordered_ids, count)
        sampled_sets[sample_set] = sampled
        payload["sample_sets"][sample_set] = {
            "seed": seed, "reference_method": "DID", "num_samples": count,
            "reference_dir": str(spec["reference_dir"]),
            "minimum_common_covered_bars_4_4": minimum_bars or None,
            "eligible_sample_count": len(ordered_ids), "sample_ids": sampled,
        }
        text_rows.extend([f"[{sample_set}]", *sampled, ""])
    (manifest_dir / "sampled_sets.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (manifest_dir / "sampled_ids.txt").write_text("\n".join(text_rows).rstrip() + "\n", encoding="utf-8")

    availability: list[dict[str, Any]] = []
    for task in config["tasks"]:
        sampled = task_sample_ids(workspace, config, task)
        for method in task["methods"]:
            index = midi_index(resolve(method["midi_dir"], config_dir))
            for sample_id in sampled:
                exists = sample_id in index
                availability.append({
                    "task_type": task["task_type"], "sample_set": task_sample_set(task),
                    "sample_id": sample_id, "method": method["name"],
                    "source_midi_exists": exists, "preprocessing_succeeded": False,
                    "audio_render_succeeded": False, "error_reason": "" if exists else "missing_source_midi",
                })
    write_csv(workspace / "reports" / "sample_availability.csv", list(availability[0]), availability)
    missing = sum(not row["source_midi_exists"] for row in availability)
    print(f"Sampled {sum(len(ids) for ids in sampled_sets.values())} IDs across {len(sampled_sets)} sets. Missing method files: {missing}.")
    if missing:
        raise SystemExit(2)


def inspect_midi_timing(source: Path) -> tuple[int, int]:
    import mido
    midi = mido.MidiFile(source)
    numerator, denominator = 4, 4
    max_note_tick = 0
    for track in midi.tracks:
        absolute = 0
        for message in track:
            absolute += message.time
            if message.type == "time_signature" and absolute == 0:
                numerator, denominator = message.numerator, message.denominator
            if message.type in {"note_on", "note_off"}:
                max_note_tick = max(max_note_tick, absolute)
    ticks_per_bar = round(midi.ticks_per_beat * numerator * 4 / denominator)
    return ticks_per_bar, max_note_tick


def tempo_and_length(
    source: Path,
    destination: Path,
    target_bpm: float,
    target_bars: float | None,
    min_bars: float | None,
    tick_scale: float = 1.0,
    target_numerator: int = 4,
    target_denominator: int = 4,
):
    try:
        import mido
    except ImportError as exc:
        raise RuntimeError("Install preprocessing dependency: pip install mido") from exc
    midi = mido.MidiFile(source)
    tempo_events: list[int] = []
    max_note_tick = 0
    numerator, denominator = 4, 4
    for track in midi.tracks:
        absolute = 0
        for message in track:
            absolute += message.time
            if message.type in {"note_on", "note_off"}:
                max_note_tick = max(max_note_tick, absolute)
            if message.type == "set_tempo": tempo_events.append(message.tempo)
            if message.type == "time_signature" and absolute == 0:
                numerator, denominator = message.numerator, message.denominator
    original_bpm = 60_000_000 / tempo_events[0] if tempo_events else 120.0
    ticks_per_bar = round(midi.ticks_per_beat * target_numerator * 4 / target_denominator)
    scaled_max_note_tick = round(max_note_tick * tick_scale)
    target_ticks = round(target_bars * ticks_per_bar) if target_bars is not None else None
    min_ticks = round(min_bars * ticks_per_bar) if min_bars is not None else None
    original_duration = midi.length
    if min_ticks is not None and scaled_max_note_tick < min_ticks:
        available_bars = scaled_max_note_tick / ticks_per_bar
        raise ValueError(f"insufficient_length: {available_bars:.3f} bars available, {min_bars:.3f} required")

    for track_index, track in enumerate(midi.tracks):
        absolute = 0
        kept: list[tuple[int, Any]] = []
        for message in track:
            absolute += message.time
            scaled_absolute = round(absolute * tick_scale)
            if message.type in {"set_tempo", "time_signature", "end_of_track"}:
                continue
            if target_ticks is None or scaled_absolute <= target_ticks:
                kept.append((scaled_absolute, message.copy(time=0)))
        if track_index == 0:
            kept.append((0, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(target_bpm), time=0)))
            kept.append((0, mido.MetaMessage("time_signature", numerator=target_numerator, denominator=target_denominator, time=0)))
        if target_ticks is not None:
            for channel in range(16):
                kept.append((target_ticks, mido.Message("control_change", channel=channel, control=123, value=0, time=0)))
        priority = {"time_signature": 0, "set_tempo": 1}
        kept.sort(key=lambda item: (item[0], priority.get(item[1].type, 2)))
        previous = 0
        rebuilt = mido.MidiTrack()
        for tick, message in kept:
            rebuilt.append(message.copy(time=max(0, tick - previous)))
            previous = tick
        midi.tracks[track_index] = rebuilt
    destination.parent.mkdir(parents=True, exist_ok=True)
    midi.save(destination)
    final_duration = mido.MidiFile(destination).length
    return original_bpm, original_duration, final_duration, ticks_per_bar, max_note_tick, f"{numerator}/{denominator}", tick_scale


def loudness(path: Path, ffmpeg: str, duration_sec: float | None = None) -> str:
    command = [ffmpeg, "-hide_banner", "-i", str(path)]
    if duration_sec is not None:
        command.extend(["-t", f"{duration_sec:.6f}"])
    command.extend(["-filter_complex", "ebur128", "-f", "null", "-"])
    result = subprocess.run(command, capture_output=True, text=True)
    matches = re.findall(r"I:\s*(-?[\d.]+) LUFS", result.stderr)
    return matches[-1] if matches else ""


def normalize_audio(
    source: Path,
    destination: Path,
    ffmpeg: str,
    target_lufs: float,
    sample_rate: int,
    input_lufs: float,
    duration_sec: float,
) -> None:
    if input_lufs == float("-inf"):
        raise RuntimeError("cannot normalize silent audio")
    destination.parent.mkdir(parents=True, exist_ok=True)
    gain_db = target_lufs - input_lufs
    normalize_filter = (
        f"volume={gain_db:.6f}dB,"
        "alimiter=limit=0.841395:attack=5:release=50:level=false"
    )
    subprocess.run([
        ffmpeg, "-y", "-i", str(source), "-af", normalize_filter,
        "-t", f"{duration_sec:.6f}", "-ar", str(sample_rate), "-ac", "2",
        "-sample_fmt", "s16", str(destination),
    ], check=True, capture_output=True, text=True)


def render_midi(source: Path, destination: Path, fluidsynth: str, soundfont: Path, sample_rate: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        fluidsynth, "-ni", "-F", str(destination), "-r", str(sample_rate),
        "-T", "wav", "-O", "s16", str(soundfont), str(source),
    ], check=True, capture_output=True, text=True)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tool_version(command: str, flag: str) -> str:
    result = subprocess.run([command, flag], capture_output=True, text=True, check=True)
    return (result.stdout or result.stderr).splitlines()[0].strip()


def preprocess_command(config: dict[str, Any], config_dir: Path) -> None:
    workspace = resolve(config["evaluation_workspace"], config_dir)
    methods = all_methods(config, config_dir)
    specs = sampling_specs(config, config_dir)
    ensure_isolated(workspace, [*(spec["reference_dir"] for spec in specs.values()), *(path for _, _, path in methods)])
    target_bpm = float(config["audio"]["target_bpm"])
    target_lufs = float(config["audio"]["target_lufs"])
    sample_rate = int(config["audio"].get("sample_rate", 44100))
    render_workers = max(1, int(config["audio"].get("render_workers", 4)))
    soundfont = resolve(config["audio"]["soundfont"], config_dir)
    fluidsynth = config["audio"].get("fluidsynth", "fluidsynth")
    ffmpeg = config["audio"].get("ffmpeg", "ffmpeg")
    if not soundfont.is_file():
        raise FileNotFoundError(f"SoundFont does not exist: {soundfont}")

    environment = {
        "target_bpm": target_bpm, "target_lufs": target_lufs, "sample_rate": sample_rate,
        "channels": 2, "bit_depth": 16, "time_signature": "4/4", "render_workers": render_workers,
        "normalization": "whole-file EBU R128 integrated loudness gain; -1.5 dBFS peak limiter",
        "prompt_policy": "preserve each source MIDI, including its original phase and notes",
        "fluidsynth": str(Path(fluidsynth).resolve()), "fluidsynth_version": tool_version(fluidsynth, "--version"),
        "ffmpeg": str(Path(ffmpeg).resolve()), "ffmpeg_version": tool_version(ffmpeg, "-version"),
        "soundfont": str(soundfont), "soundfont_sha256": file_sha256(soundfont),
    }
    environment_path = workspace / "manifests" / "rendering_environment.json"
    environment_path.parent.mkdir(parents=True, exist_ok=True)
    environment_path.write_text(json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8")

    jobs = []
    for task in config["tasks"]:
        sampled = task_sample_ids(workspace, config, task)
        kind = task_kind(task)
        task_type = task["task_type"]
        accompaniment_bars: dict[str, float] = {}
        if kind == "accompaniment":
            length_reference_dir = resolve(task.get("length_reference_midi_dir", config["reference_midi_dir"]), config_dir)
            length_reference = midi_index(length_reference_dir)
            for sample_id in sampled:
                try:
                    reference_ticks_per_bar, reference_note_ticks = inspect_midi_timing(length_reference[sample_id])
                    accompaniment_bars[sample_id] = float(task.get(
                        "evaluation_length_bars",
                        reference_note_ticks / reference_ticks_per_bar,
                    ))
                except Exception as exc:
                    raise RuntimeError(f"Cannot establish accompaniment DID reference length for {sample_id}: {exc}") from exc
        for method in task["methods"]:
            index = midi_index(resolve(method["midi_dir"], config_dir))
            for sample_id in sampled:
                jobs.append((task, method, sample_id, index.get(sample_id), accompaniment_bars))

    def process_one(job) -> dict[str, Any]:
        import mido
        task, method, sample_id, source, accompaniment_bars = job
        task_type = task["task_type"]
        kind = task_kind(task)
        method_name = method["name"]
        prompt_bars = int(task.get("prompt_length_bars", 0))
        row = {field: "" for field in REPORT_FIELDS}
        row.update({"task_type": task_type, "sample_id": sample_id, "method": method_name, "target_bpm": target_bpm, "prompt_length_bars": prompt_bars})
        try:
            if source is None:
                raise FileNotFoundError("missing_source_midi")
            row["source_path"] = str(source)
            processed = workspace / "midi_processed" / task_type / method_name / f"{sample_id}.mid"
            opaque = hashlib.sha256(f"{task_type}:{sample_id}:{method_name}".encode()).hexdigest()[:16]
            raw_audio = workspace / "audio_raw" / task_type / sample_id / f"{opaque}.wav"
            audio = workspace / "audio" / task_type / sample_id / f"{opaque}.wav"
            tick_scale = float(method.get("time_scale", 1.0))
            _, note_ticks = inspect_midi_timing(source)
            source_bars = note_ticks * tick_scale / (mido.MidiFile(source).ticks_per_beat * 4)
            row["source_bars_4_4"] = round(source_bars, 4)
            row["prompt_policy"] = "original_source_preserved"
            if kind == "continuation":
                target_bars = float(task.get("evaluation_length_bars", 32))
                # Fixed-length exports can place the final note-off one beat early.
                min_bars = target_bars - float(task.get("length_tolerance_bars", 0.25))
                if source_bars < min_bars:
                    if not task.get("allow_short_preview", False):
                        raise ValueError(f"insufficient_length: {source_bars:.3f} bars, {target_bars:.3f} required")
                    row["length_warning"] = f"source_shorter_than_{target_bars:g}_bars; development_preview_only"
                    target_bars = source_bars
                    min_bars = None
                row["continuation_length_bars"] = max(0, target_bars - prompt_bars)
                # The configured prompt length is nominal; codec-specific boundaries
                # are audited separately and are never synthesized or replaced here.
                row["final_length_bars"] = target_bars
            else:
                target_bars = accompaniment_bars[sample_id]
                min_bars = target_bars
            processed.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, processed)
            original_bpm, original_duration, final_duration, _, _, source_signature, tick_scale = tempo_and_length(
                processed, processed, target_bpm, target_bars, min_bars, tick_scale=tick_scale,
            )
            if kind == "accompaniment":
                row["final_length_bars"] = round(accompaniment_bars[sample_id], 3)
            row.update({
                "processed_midi_path": str(processed), "original_bpm": round(original_bpm, 3),
                "original_duration_sec": round(original_duration, 3), "final_duration_sec": round(final_duration, 3),
                "source_time_signature": source_signature, "tick_scale_factor": round(tick_scale, 6),
            })
            render_midi(processed, raw_audio, fluidsynth, soundfont, sample_rate)
            row["loudness_before"] = loudness(raw_audio, ffmpeg, final_duration)
            normalize_audio(
                raw_audio, audio, ffmpeg, target_lufs, sample_rate,
                float(row["loudness_before"]), final_duration,
            )
            row.update({"audio_path": str(audio), "loudness_after": loudness(audio, ffmpeg), "status": "ok"})
        except Exception as exc:
            row.update({"status": "insufficient_length" if "insufficient_length" in str(exc) else "error", "error_message": str(exc)})
        return row

    with ThreadPoolExecutor(max_workers=render_workers) as executor:
        rows = []
        for index, row in enumerate(executor.map(process_one, jobs), start=1):
            rows.append(row)
            print(f"[{index:03d}] {row['task_type']}/{row['method']}/{row['sample_id']}: {row['status']}", flush=True)
    current_total = len(rows)
    failures = sum(row["status"] != "ok" for row in rows)
    report_path = workspace / "reports" / "preprocessing_report.csv"
    if config.get("_preserve_other_task_reports") and report_path.is_file():
        with report_path.open(encoding="utf-8-sig", newline="") as handle:
            previous_rows = list(csv.DictReader(handle))
        active_task_types = {task["task_type"] for task in config["tasks"]}
        rows = [row for row in previous_rows if row["task_type"] not in active_task_types] + rows
    write_csv(report_path, REPORT_FIELDS, rows)
    print(f"Preprocessed {current_total - failures}/{current_total} files. Failures: {failures}.")
    if failures: raise SystemExit(3)


def renormalize_command(config: dict[str, Any], config_dir: Path) -> None:
    preprocess_command(config, config_dir)


def web_manifest_command(config: dict[str, Any], config_dir: Path) -> None:
    workspace = resolve(config["evaluation_workspace"], config_dir)
    specs = sampling_specs(config, config_dir)
    ensure_isolated(workspace, [*(spec["reference_dir"] for spec in specs.values()), *(path for _, _, path in all_methods(config, config_dir))])
    with (workspace / "reports" / "preprocessing_report.csv").open(encoding="utf-8-sig", newline="") as handle:
        report = list(csv.DictReader(handle))
    lookup = {(row["task_type"], row["sample_id"], row["method"]): row for row in report}
    ffmpeg = config["audio"].get("ffmpeg", "ffmpeg")
    tasks: list[dict[str, Any]] = []
    failures: list[str] = []
    for task in config["tasks"]:
        web_bitrate = task.get("web_bitrate", config["audio"].get("web_bitrate", "192k"))
        groups = []
        sampling_groups = task_sampling_groups(workspace, config, task)
        for sampling_group in sampling_groups:
            for sample_id in sampling_group["sample_ids"]:
                samples = []
                for method in task["methods"]:
                    key = (task["task_type"], sample_id, method["name"])
                    row = lookup.get(key)
                    if not row or row["status"] != "ok":
                        failures.append("/".join(key))
                        continue
                    source_audio = Path(row["audio_path"])
                    # Content-addressed URLs prevent browsers/CDNs serving older audio
                    # after timbre, tempo or length corrections.
                    source_hash = file_sha256(source_audio)
                    web_hash = hashlib.sha256(
                        f"{source_hash}:{web_bitrate}".encode()
                    ).hexdigest()[:24]
                    filename = f"{web_hash}.mp3"
                    web_audio = workspace / "web_audio" / task["task_type"] / sample_id / filename
                    web_audio.parent.mkdir(parents=True, exist_ok=True)
                    if not web_audio.exists() or web_audio.stat().st_mtime_ns < source_audio.stat().st_mtime_ns:
                        subprocess.run([
                            ffmpeg, "-y", "-i", str(source_audio), "-codec:a", "libmp3lame",
                            "-b:a", str(web_bitrate), "-ar", str(config["audio"].get("sample_rate", 44100)),
                            "-ac", "2", str(web_audio),
                        ], check=True, capture_output=True, text=True)
                    samples.append({"model": method["name"], "audio_url": f"/audio/{task['task_type']}/{sample_id}/{filename}"})
                reference = next((sample for sample in samples if sample['model'] == 'GroundTruth'), None)
                if reference is None:
                    failures.append(f"{task['task_type']}/{sample_id}/missing_GT_reference")
                group_id = (
                    f"{task['task_type']}_{sample_id}"
                    if sampling_group["id"] == "primary"
                    else f"{task['task_type']}__{sampling_group['id']}__{sample_id}"
                )
                groups.append({
                    "group_id": group_id,
                    "sample_id": sample_id,
                    "sampling_group_id": sampling_group["id"],
                    "sampling_group_title": sampling_group["title"],
                    "reference": reference,
                    "samples": [sample for sample in samples if sample['model'] != 'GroundTruth'],
                })
        tasks.append({
            "task_type": task["task_type"], "title": task["title"],
            "short_description": task["short_description"], "rules_markdown": task["rules_markdown"],
            "metrics": task["metrics"],
            "sampling_groups": [{"id": item["id"], "title": item["title"], "sample_count": len(item["sample_ids"])} for item in sampling_groups],
            "groups": groups,
        })
    if failures:
        raise ValueError("Cannot build manifest; preprocessing is incomplete for: " + ", ".join(failures))
    output = workspace / "web_data" / "evaluation_manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    title_by_type = {task["task_type"]: task["title"] for task in config["tasks"]}
    short_sources = sorted({
        f"{title_by_type.get(row['task_type'], row['task_type'])}的 {row['method']}"
        for row in report if row.get("length_warning")
    })
    payload = {"study_id": config.get("study_id", "pop909_subjective_evaluation"), "phase": "development", "version": config.get("version", "1.0.0"),
               "audio_delivery": "local_package",
               "show_model_names": bool(config.get("show_model_names", False)),
               "allow_model_reveal_after_scoring": bool(config.get("allow_model_reveal_after_scoring", False)),
               "quality_notice": f"{'、'.join(short_sources)} 部分源文件短于目标长度，当前仅供检查试听，正式评测前需补齐源 MIDI。" if short_sources else "", "tasks": tasks}
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote web manifest: {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["sample", "preprocess", "renormalize", "web-manifest"])
    parser.add_argument("--config", default="evaluation.config.json")
    parser.add_argument("--task-type", help="Only preprocess or renormalize one task and preserve other task report rows")
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    config = read_config(config_path)
    if args.task_type:
        if args.command not in {"preprocess", "renormalize"}:
            parser.error("--task-type is supported only by preprocess and renormalize")
        selected = [task for task in config["tasks"] if task["task_type"] == args.task_type]
        if not selected:
            parser.error(f"unknown task_type: {args.task_type}")
        config = {**config, "tasks": selected, "_preserve_other_task_reports": True}
    {"sample": sample_command, "preprocess": preprocess_command, "renormalize": renormalize_command, "web-manifest": web_manifest_command}[args.command](config, config_path.parent)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
