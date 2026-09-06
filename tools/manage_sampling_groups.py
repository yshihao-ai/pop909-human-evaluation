#!/usr/bin/env python3
"""Desktop manager for adding explicit MIDI batches to an evaluation task."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import prepare_evaluation as prep


def read_state(workspace: Path) -> dict[str, Any]:
    path = workspace / "manifests" / "task_sampling_groups.json"
    if not path.is_file():
        return {"format": prep.SAMPLING_GROUPS_FORMAT, "tasks": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format") != prep.SAMPLING_GROUPS_FORMAT:
        raise ValueError(f"不支持的抽样组文件格式：{path}")
    payload.setdefault("tasks", {})
    return payload


def write_state(workspace: Path, payload: dict[str, Any]) -> None:
    path = workspace / "manifests" / "task_sampling_groups.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def candidate_ids(task: dict[str, Any], config_dir: Path) -> list[str]:
    indices = [prep.midi_index(prep.resolve(method["midi_dir"], config_dir)) for method in task["methods"]]
    if not indices:
        return []
    common = set(indices[0])
    for index in indices[1:]:
        common.intersection_update(index)
    coverage_bars = int(task.get("minimum_common_covered_bars_4_4", 0))
    if coverage_bars:
        def has_coverage(sample_id: str) -> bool:
            return all(prep.covered_bars_4_4(index[sample_id]) >= coverage_bars for index in indices)

        ordered = sorted(common, key=str.casefold)
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(ordered)))) as executor:
            common = {sample_id for sample_id, accepted in zip(ordered, executor.map(has_coverage, ordered)) if accepted}
    elif not task.get("allow_short_preview", False):
        minimum_bars = float(task.get("evaluation_length_bars", 0))
        if prep.task_kind(task) == "continuation":
            minimum_bars -= float(task.get("length_tolerance_bars", 0))
        import mido

        def has_length(path: Path) -> bool:
            midi = mido.MidiFile(path)
            max_note_tick = 0
            for track in midi.tracks:
                absolute = 0
                for message in track:
                    absolute += message.time
                    if message.type in {"note_on", "note_off"}:
                        max_note_tick = max(max_note_tick, absolute)
            return max_note_tick / (midi.ticks_per_beat * 4) >= minimum_bars

        def all_methods_have_length(sample_id: str) -> bool:
            return all(has_length(index[sample_id]) for index in indices)

        ordered = sorted(common, key=str.casefold)
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(ordered)))) as executor:
            common = {sample_id for sample_id, accepted in zip(ordered, executor.map(all_methods_have_length, ordered)) if accepted}
    return sorted(common, key=str.casefold)


def next_group_id(existing: list[dict[str, Any]]) -> str:
    used = {str(item.get("id", "")) for item in existing}
    index = 2
    while f"batch_{index:02d}" in used:
        index += 1
    return f"batch_{index:02d}"


def run_step(command: list[str], cwd: Path, log: Callable[[str], None]) -> None:
    log("\n▶ " + " ".join(command))
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        log(line.rstrip())
    if process.wait() != 0:
        raise RuntimeError(f"步骤执行失败（退出码 {process.returncode}）：{' '.join(command)}")


def add_and_build(
    config_path: Path,
    task_type: str,
    title: str,
    sample_ids: list[str],
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    config_path = config_path.resolve()
    project_dir = config_path.parent
    config = prep.read_config(config_path)
    task = next((item for item in config["tasks"] if item["task_type"] == task_type), None)
    if task is None:
        raise ValueError(f"未知任务：{task_type}")
    title = title.strip()
    sample_ids = list(dict.fromkeys(value.strip() for value in sample_ids if value.strip()))
    if not title:
        raise ValueError("请填写抽样组名称。")
    if not sample_ids:
        raise ValueError("请至少选择一个 MIDI。")

    available = set(candidate_ids(task, project_dir))
    missing = [sample_id for sample_id in sample_ids if sample_id not in available]
    if missing:
        raise ValueError("这些 MIDI 没有被所有参评方法共同提供：" + "、".join(missing))

    workspace = prep.resolve(config["evaluation_workspace"], project_dir)
    previous = read_state(workspace)
    updated = json.loads(json.dumps(previous, ensure_ascii=False))
    task_groups = updated["tasks"].setdefault(task_type, [])
    existing_groups = prep.task_sampling_groups(workspace, config, task)
    used_samples = {sample_id for group in existing_groups for sample_id in group["sample_ids"]}
    repeated = [sample_id for sample_id in sample_ids if sample_id in used_samples]
    if repeated:
        raise ValueError("这些 MIDI 已经属于当前任务的抽样组：" + "、".join(repeated))
    group_id = next_group_id(task_groups)
    task_groups.append({
        "id": group_id,
        "title": title,
        "sample_ids": sample_ids,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })

    write_state(workspace, updated)
    try:
        python = sys.executable
        run_step([python, "tools/prepare_evaluation.py", "preprocess", "--config", str(config_path), "--task-type", task_type], project_dir, log)
        run_step([python, "tools/prepare_evaluation.py", "web-manifest", "--config", str(config_path)], project_dir, log)
        generated_manifest = workspace / "web_data" / "evaluation_manifest.json"
        public_manifest = project_dir / "public" / "data" / "evaluation_manifest.json"
        public_manifest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(generated_manifest, public_manifest)
        log(f"已更新网页清单：{public_manifest}")
        run_step([python, "tools/verify_evaluation.py", "--config", str(config_path)], project_dir, log)
        run_step([python, "tools/package_local_audio.py", "--config", str(config_path), "--task-type", task_type], project_dir, log)
        npm = shutil.which("npm.cmd") or shutil.which("npm")
        if not npm:
            raise RuntimeError("找不到 npm，无法验证网页构建。")
        run_step([npm, "run", "build"], project_dir, log)
    except Exception:
        write_state(workspace, previous)
        log("构建失败，已恢复添加前的抽样组配置。")
        try:
            run_step([sys.executable, "tools/prepare_evaluation.py", "web-manifest", "--config", str(config_path)], project_dir, log)
            shutil.copy2(workspace / "web_data" / "evaluation_manifest.json", project_dir / "public" / "data" / "evaluation_manifest.json")
        except Exception as rollback_error:
            log(f"恢复网页清单时发生错误：{rollback_error}")
        raise

    archive_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", f"pop909_eval_audio_{task_type}_{config.get('version', '1.0.0')}").strip("_")
    archive = workspace / "packages" / f"{archive_stem}.zip"
    all_archive_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", f"pop909_eval_audio_all_tasks_{config.get('version', '1.0.0')}").strip("_")
    all_archive = workspace / "packages" / f"{all_archive_stem}.zip"
    return {
        "task_type": task_type,
        "group_id": group_id,
        "title": title,
        "sample_ids": sample_ids,
        "archive": str(archive) if archive.is_file() else "",
        "all_archive": str(all_archive) if all_archive.is_file() else "",
    }


class ManagerApp:
    def __init__(self, config_path: Path) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.config_path = config_path.resolve()
        self.project_dir = self.config_path.parent
        self.config = prep.read_config(self.config_path)
        self.workspace = prep.resolve(self.config["evaluation_workspace"], self.project_dir)
        self.tasks = self.config["tasks"]
        self.task_labels = {f"{item['title']}（{item['task_type']}）": item["task_type"] for item in self.tasks}
        self.root = tk.Tk()
        self.root.title("评测抽样组管理器")
        self.root.geometry("900x720")
        self.root.minsize(760, 620)

        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(5, weight=1)
        frame.rowconfigure(8, weight=1)

        ttk.Label(frame, text="新增评测抽样组", font=("Microsoft YaHei UI", 16, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="选择一个任务和若干 MIDI；工具会匹配该任务的全部模型、渲染 192 kbps 音频、更新网页清单和任务数据包。", wraplength=820).grid(row=1, column=0, sticky="ew", pady=(4, 14))

        controls = ttk.Frame(frame)
        controls.grid(row=2, column=0, sticky="ew")
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="评测任务").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.task_value = tk.StringVar(value=next(iter(self.task_labels)))
        self.task_box = ttk.Combobox(controls, state="readonly", textvariable=self.task_value, values=list(self.task_labels))
        self.task_box.grid(row=0, column=1, sticky="ew")
        self.task_box.bind("<<ComboboxSelected>>", lambda _event: self.refresh_candidates())
        ttk.Label(controls, text="抽样组名称").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        self.title_value = tk.StringVar()
        ttk.Entry(controls, textvariable=self.title_value).grid(row=1, column=1, sticky="ew", pady=(10, 0))

        search_row = ttk.Frame(frame)
        search_row.grid(row=3, column=0, sticky="ew", pady=(14, 6))
        search_row.columnconfigure(0, weight=1)
        self.search_value = tk.StringVar()
        self.search_value.trace_add("write", lambda *_args: self.apply_filter())
        ttk.Entry(search_row, textvariable=self.search_value).grid(row=0, column=0, sticky="ew")
        self.count_label = ttk.Label(search_row, text="")
        self.count_label.grid(row=0, column=1, padx=(10, 0))
        ttk.Label(frame, text="可选 MIDI 名称（Ctrl/Shift 多选；已经使用过的名称不会再次列出）").grid(row=4, column=0, sticky="w")

        list_frame = ttk.Frame(frame)
        list_frame.grid(row=5, column=0, sticky="nsew", pady=(6, 10))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(list_frame, selectmode="extended", exportselection=False, font=("Consolas", 10))
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=scrollbar.set)

        self.build_button = ttk.Button(frame, text="新增并自动构建", command=self.start_build)
        self.build_button.grid(row=6, column=0, sticky="ew", pady=(0, 12))
        self.status_value = tk.StringVar(value="准备就绪")
        ttk.Label(frame, textvariable=self.status_value).grid(row=7, column=0, sticky="w")
        self.log_box = tk.Text(frame, height=10, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_box.grid(row=8, column=0, sticky="nsew", pady=(6, 0))

        self.all_candidates: list[str] = []
        self.visible_candidates: list[str] = []
        self.candidate_cache: dict[str, list[str]] = {}
        self.load_generation = 0
        self.refresh_candidates()

    def task(self) -> dict[str, Any]:
        task_type = self.task_labels[self.task_value.get()]
        return next(item for item in self.tasks if item["task_type"] == task_type)

    def refresh_candidates(self) -> None:
        task = self.task()
        groups = prep.task_sampling_groups(self.workspace, self.config, task)
        used = {sample_id for group in groups for sample_id in group["sample_ids"]}
        self.title_value.set(f"抽样组 {len(groups) + 1}")
        self.load_generation += 1
        generation = self.load_generation
        self.build_button.configure(state="disabled")
        self.status_value.set(f"正在读取“{task['title']}”的可用 MIDI……")
        cached = self.candidate_cache.get(task["task_type"])
        if cached is not None:
            self._finish_candidate_load(generation, task, groups, used, cached, None)
            return
        threading.Thread(
            target=self._candidate_worker,
            args=(generation, task, groups, used),
            daemon=True,
        ).start()

    def _candidate_worker(self, generation: int, task: dict[str, Any], groups: list[dict[str, Any]], used: set[str]) -> None:
        try:
            candidates = candidate_ids(task, self.project_dir)
            error = None
        except Exception as exc:
            candidates = []
            error = str(exc)
        self.root.after(0, self._finish_candidate_load, generation, task, groups, used, candidates, error)

    def _finish_candidate_load(self, generation: int, task: dict[str, Any], groups: list[dict[str, Any]], used: set[str], candidates: list[str], error: str | None) -> None:
        if generation != self.load_generation or task["task_type"] != self.task()["task_type"]:
            return
        if error:
            self.all_candidates = []
            self.apply_filter()
            self.status_value.set("读取 MIDI 失败：" + error)
            return
        self.candidate_cache[task["task_type"]] = candidates
        self.all_candidates = [sample_id for sample_id in candidates if sample_id not in used]
        self.apply_filter()
        self.build_button.configure(state="normal")
        self.status_value.set(f"{task['title']}：已有 {len(groups)} 个抽样组，可新增 {len(self.all_candidates)} 个 MIDI")

    def apply_filter(self) -> None:
        query = self.search_value.get().strip().casefold()
        self.visible_candidates = [value for value in self.all_candidates if query in value.casefold()]
        self.listbox.delete(0, "end")
        for value in self.visible_candidates:
            self.listbox.insert("end", value)
        self.count_label.configure(text=f"显示 {len(self.visible_candidates)} / {len(self.all_candidates)}")

    def log(self, text: str) -> None:
        self.root.after(0, self._append_log, text)

    def _append_log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def start_build(self) -> None:
        from tkinter import messagebox

        selected = [self.visible_candidates[index] for index in self.listbox.curselection()]
        if not selected:
            messagebox.showwarning("尚未选择 MIDI", "请至少选择一个 MIDI 名称。", parent=self.root)
            return
        title = self.title_value.get().strip()
        if not title:
            messagebox.showwarning("缺少名称", "请填写抽样组名称。", parent=self.root)
            return
        if not messagebox.askokcancel("开始构建", f"向“{self.task()['title']}”新增“{title}”，共 {len(selected)} 首 MIDI？\n\n渲染期间请不要关闭窗口。", parent=self.root):
            return
        self.build_button.configure(state="disabled")
        self.status_value.set("正在渲染和构建，请稍候……")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        threading.Thread(target=self._build_worker, args=(self.task()["task_type"], title, selected), daemon=True).start()

    def _build_worker(self, task_type: str, title: str, sample_ids: list[str]) -> None:
        from tkinter import messagebox

        try:
            result = add_and_build(self.config_path, task_type, title, sample_ids, self.log)
        except Exception as error:
            message = str(error)
            self.root.after(0, lambda: self.status_value.set("构建失败，已恢复原配置"))
            self.root.after(0, lambda: self.build_button.configure(state="normal"))
            self.root.after(0, lambda message=message: messagebox.showerror("构建失败", message, parent=self.root))
            return
        self.root.after(0, self.refresh_candidates)
        self.root.after(0, lambda: self.build_button.configure(state="normal"))
        self.root.after(0, lambda: self.status_value.set(f"完成：{result['title']}（{len(result['sample_ids'])} 首）"))
        self.root.after(0, lambda: messagebox.showinfo("构建完成", f"新抽样组已经加入网页清单。\n“全部任务”总包也已更新：\n{result['all_archive']}", parent=self.root))

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description="选择 MIDI 名称并向指定评测任务新增抽样组")
    parser.add_argument("--config", default="evaluation.config.json")
    parser.add_argument("--list-json", action="store_true", help="Print tasks, current groups, and candidate counts without opening the GUI")
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    if args.list_json:
        config = prep.read_config(config_path)
        workspace = prep.resolve(config["evaluation_workspace"], config_path.parent)
        result = []
        for task in config["tasks"]:
            groups = prep.task_sampling_groups(workspace, config, task)
            used = {sample_id for group in groups for sample_id in group["sample_ids"]}
            candidates = candidate_ids(task, config_path.parent)
            result.append({
                "task_type": task["task_type"],
                "title": task["title"],
                "groups": groups,
                "available_new_sample_count": sum(sample_id not in used for sample_id in candidates),
            })
        print(json.dumps({"tasks": result}, ensure_ascii=False, indent=2))
        return
    ManagerApp(config_path).run()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
