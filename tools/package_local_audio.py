#!/usr/bin/env python3
"""Build verified per-task packages plus one package containing every task."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any


PACKAGE_FORMAT = "pop909-local-audio-v1"
ALL_TASKS_PACKAGE_FORMAT = "pop909-local-audio-v2"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")


def task_audio_urls(task: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for group in task["groups"]:
        if group.get("reference"):
            urls.append(group["reference"]["audio_url"])
        urls.extend(sample["audio_url"] for sample in group["samples"])
    return list(dict.fromkeys(urls))


def task_bitrate(config: dict[str, Any], task_type: str) -> str:
    return next(
        item.get("web_bitrate", config["audio"].get("web_bitrate", "192k"))
        for item in config["tasks"] if item["task_type"] == task_type
    )


def task_files(workspace: Path, task: dict[str, Any]) -> list[dict[str, Any]]:
    files = []
    for url in task_audio_urls(task):
        relative = url.lstrip("/").replace("\\", "/")
        source = workspace / "web_audio" / relative.removeprefix("audio/")
        if not source.is_file():
            raise FileNotFoundError(f"Missing packaged audio: {source}")
        files.append({
            "path": relative,
            "size_bytes": source.stat().st_size,
            "sha256": sha256(source),
        })
    return files


def write_archive(workspace: Path, archive: Path, root: str, package_manifest: dict[str, Any]) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
        bundle.writestr(
            f"{root}/package_manifest.json",
            json.dumps(package_manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        for item in package_manifest["files"]:
            source = workspace / "web_audio" / item["path"].removeprefix("audio/")
            bundle.write(source, f"{root}/{item['path']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="evaluation.config.json")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--task-type", help="Build only one task package")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    workspace = resolve(config["evaluation_workspace"], config_path.parent)
    manifest_path = workspace / "web_data" / "evaluation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output_dir = args.output_dir.resolve() if args.output_dir else workspace / "packages"
    output_dir.mkdir(parents=True, exist_ok=True)
    report = output_dir / "package_report.json"

    files_by_task = {task["task_type"]: task_files(workspace, task) for task in manifest["tasks"]}
    rebuilt_summaries: list[dict[str, Any]] = []
    selected_tasks = [
        task for task in manifest["tasks"]
        if not args.task_type or task["task_type"] == args.task_type
    ]
    if args.task_type and not selected_tasks:
        raise ValueError(f"Unknown task_type: {args.task_type}")
    if args.task_type and not report.is_file():
        selected_tasks = manifest["tasks"]
    for task in selected_tasks:
        bitrate = task_bitrate(config, task["task_type"])
        files = files_by_task[task["task_type"]]

        package_manifest = {
            "package_format": PACKAGE_FORMAT,
            "manifest_version": manifest["version"],
            "task_type": task["task_type"],
            "task_title": task["title"],
            "audio_bitrate": bitrate,
            "file_count": len(files),
            "files": files,
        }
        root = safe_name(f"pop909_eval_audio_{task['task_type']}_{manifest['version']}")
        archive = output_dir / f"{root}.zip"
        write_archive(workspace, archive, root, package_manifest)
        rebuilt_summaries.append({
            "task_type": task["task_type"],
            "scope": "single_task",
            "file_count": len(files),
            "archive": str(archive),
            "size_bytes": archive.stat().st_size,
            "sha256": sha256(archive),
        })

    task_entries = [{
        "task_type": task["task_type"],
        "task_title": task["title"],
        "audio_bitrate": task_bitrate(config, task["task_type"]),
        "file_count": len(files_by_task[task["task_type"]]),
    } for task in manifest["tasks"]]
    all_files = [item for task in manifest["tasks"] for item in files_by_task[task["task_type"]]]
    if len({item["path"] for item in all_files}) != len(all_files):
        raise ValueError("Duplicate audio path found across evaluation tasks")
    all_manifest = {
        "package_format": ALL_TASKS_PACKAGE_FORMAT,
        "manifest_version": manifest["version"],
        "scope": "all_tasks",
        "task_count": len(task_entries),
        "tasks": task_entries,
        "file_count": len(all_files),
        "files": all_files,
    }
    all_root = safe_name(f"pop909_eval_audio_all_tasks_{manifest['version']}")
    all_archive = output_dir / f"{all_root}.zip"
    write_archive(workspace, all_archive, all_root, all_manifest)
    all_summary = {
        "task_type": "all_tasks",
        "scope": "all_tasks",
        "task_count": len(task_entries),
        "file_count": len(all_files),
        "archive": str(all_archive),
        "size_bytes": all_archive.stat().st_size,
        "sha256": sha256(all_archive),
    }

    previous = json.loads(report.read_text(encoding="utf-8")).get("packages", []) if report.is_file() else []
    summary_by_task = {
        item["task_type"]: item for item in previous
        if item.get("task_type") not in {"all_tasks", *(task["task_type"] for task in selected_tasks)}
    }
    summary_by_task.update({item["task_type"]: item for item in rebuilt_summaries})
    summaries = [summary_by_task[task["task_type"]] for task in manifest["tasks"] if task["task_type"] in summary_by_task]
    summaries.append(all_summary)
    report.write_text(json.dumps({"packages": summaries}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report), "packages": summaries}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
