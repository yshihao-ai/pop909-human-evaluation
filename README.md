# POP909 与 Pop1k7 人工主观评测系统

本项目包含两个完全分离的部分：浏览器中的正式评测界面，以及仅在开发阶段运行的离线 MIDI 数据准备工具。原始 MIDI 目录始终按只读数据源处理。

## 在线访问

GitHub Pages 会在 `main` 分支更新后自动构建并发布网页。公开仓库不包含 MP3、WAV、评测结果、本机路径配置或完整音频数据包；评测者仍需另外取得“全部任务”音频 ZIP，解压后在网页中点击“打开任务文件夹”，一次加载所有任务和抽样组。

## 本地启动

```powershell
npm install
npm run dev
```

网页从 `public/data/evaluation_manifest.json` 读取任务、分组、音频路径与评分维度，并从 manifest 指向的 `.md` 文件读取规则。POP909、Pop1k7 短续写与 Pop1k7 长程生成分别建立可复现抽样集合；评测集仍处于冻结前的检查阶段。

## 可复现抽样

1. 复制 `evaluation.config.example.json` 为 `evaluation.config.json`，填写 DID、各方法 MIDI 目录、统一 SoundFont 和参数。
2. 安装 `requirements-preprocess.txt`，并确保 FluidSynth 与 FFmpeg 可用。
3. 运行：

```powershell
python tools/prepare_evaluation.py sample --config evaluation.config.json
```

工具会按 `sample_set` 稳定排序 DID 文件名，再以各数据集配置的 seed 不重复抽样，只向 `evaluation_workspace` 写入 `sampled_sets.json`、`sampled_ids.txt` 与 `sample_availability.csv`。共享同一个 `sample_set` 的任务复用同一批曲目；任一方法缺样本都会显式失败。长程任务还可通过 `minimum_common_covered_bars_4_4` 限定所有参评方法共同覆盖的最少 4/4 小节数，再从合格集合中抽样。

## MIDI 与音频预处理

```powershell
python tools/prepare_evaluation.py preprocess --config evaluation.config.json
```

新增或修正单个任务时，可用 `--task-type` 只重建该任务，同时保留报告中其他任务的记录。例如：

```powershell
python tools/prepare_evaluation.py preprocess --config evaluation.config.json --task-type continuation_pop1k7_long
```

固定处理顺序为：原始 MIDI → 工作区副本 → 120 BPM → 短续写取 16 个 4/4 小节、长程生成取 64 个 4/4 小节、伴奏取 32 个 4/4 小节 → MS Basic SoundFont 渲染 → 整段 EBU R128 响度归一化与峰值保护 → 统一 192 kbps MP3。续写完整保留各方法自身的 4 小节 Prompt、音符间隔和起始相位，并保留其后生成内容；不拼接 GT，也不根据拍号元数据拉伸音符。当前全部方法 `time_scale=1`。在 120 BPM 下，16 小节为 32 秒，64 小节为 128 秒。

续写源文件明显不足 16 小节时，默认以 `insufficient_length` 拒绝处理；检查阶段可显式开启 `allow_short_preview`，保留真实较短时长并写入 `length_warning`，页面提示不能用于正式评测。当前 BEAT 文件约 64 拍（32 个 2/4 小节、16 个 4/4 小节），正好满足本次目标；MuseTok 部分文件约 32 拍，仍只有 8 个 4/4 小节。不会通过倍速拉伸、循环或拼接补足长度。伴奏保持 32 个 4/4 小节。

每组首先展示 GT 原曲参考，GT 不评分；其余生成方法随机匿名排序并纳入平均分。Prompt 审计只进行只读比较，容许时间偏移后报告音高/起音差异，不据此修改模型输出。相位对齐仅用于分析。比较 GT 前四小节时若源方法实际仅提供更短 Prompt，该指标还会包含生成内容，不能直接解释为编码错误。评分按评测版本隔离保存，旧评分保留在原浏览器存储中，不混入本版统计。

处理报告位于 `evaluation_workspace/reports/preprocessing_report.csv`。只有全部状态为 `ok` 且没有 `length_warning` 的样本才可用于正式评测。检查版本可包含带警告的短源文件。网页音频使用内容哈希文件名，避免泄漏模型身份并防止旧音频缓存。

全部预处理通过后，可运行 `python tools/prepare_evaluation.py web-manifest --config evaluation.config.json`，在 `evaluation_workspace/web_data` 生成网页配置。该命令遇到任何缺失或失败样本都会停止；存在长度警告时仅生成带提示的 development 配置。随后运行 `python tools/verify_evaluation.py --config evaluation.config.json` 验证源音符事件、BPM、实际音频长度及 GT 参考完整性。

网页不托管评测音频。运行 `python tools/package_local_audio.py --config evaluation.config.json` 会在 `evaluation_workspace/packages` 生成一个包含全部任务、全部抽样组、对应网页评测配置和音频的 `pop909_eval_audio_all_tasks_*.zip`，同时保留四个单任务 ZIP 供开发调试。正式评测时只需分发并解压“全部任务”总包；评测者在网页中选择一次解压后的顶层文件夹，四个任务的所有抽样组便会同时加载。网页会在本地核对配置、任务数量、文件数量、大小和 SHA-256；验证通过后用本地文件播放，音频不会上传到网站。

## 自助新增抽样组

双击项目根目录的 `manage_sampling_groups.cmd`，即可打开“评测抽样组管理器”。在窗口中：

1. 选择四个评测任务之一。
2. 填写新抽样组名称。
3. 按 MIDI 文件名搜索，并使用 `Ctrl` 或 `Shift` 多选需要的曲目。
4. 点击“新增并自动构建”。

工具只列出该任务所有参评方法共同存在、达到任务长度要求、且尚未用于该任务的 MIDI 名称。确认后会只重建所选任务的 MIDI/音频，随后更新网页 manifest、该任务的 192 kbps 本地音频包和“全部任务”总包，并验证网页构建；其他三个任务不会重新渲染。

网页仍保持四个任务不变。新增内容显示为任务内的另一个命名抽样组，评测者可在任务标题右侧切换；导出的逐条记录会附带 `sampling_group_id` 和 `sampling_group_title`，任务模型平均分默认汇总该任务的全部抽样组。初始抽样的 `group_id` 保持不变，因此已有本机评分不会因启用此功能失效。

管理员工具会把最新任务与抽样组配置写入新音频包。完成本次兼容升级并发布网页后，今后新增抽样组只需把最新版“全部任务”总包发给评测者，不必再次发布网页；评测者打开原网址并选择新版任务文件夹即可。

## 冻结正式评测集

当前版本明确处于 `development` 阶段。收到“冻结当前评测样本，删除随机抽样功能”的指令后，再将确认过的 10 个 ID 固定为 `frozen_evaluation_manifest.json`，删除离线抽样能力，并令网页只读取冻结 manifest。

## 评分数据

评分按 evaluator ID 保存在浏览器 Local Storage。页面支持导出逐条评分 CSV、模型平均分 CSV 和包含二者的完整 JSON。平均分按任务与真实模型分别统计，包含各维度均值、全部维度综合均值和有效样本数；逐条明细仍保持“一行对应 evaluator 对一个模型样本的完整评分”。纯前端无法防止技术人员检查已下载资源；页面和音频 URL 不显示模型名，正式部署如需更强盲测保护应增加后端映射与受控音频分发。
