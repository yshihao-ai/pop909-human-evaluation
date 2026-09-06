'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft, ArrowRight, Check, ChevronRight, CircleAlert, Download,
  FileJson, FolderOpen, Headphones, Info, LoaderCircle, Music2, PackageCheck,
  Pause, Play, RotateCcw, Save, ShieldCheck,
} from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Progress, ProgressLabel, ProgressValue } from '@/components/ui/progress';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  type AnonymousSample, type Manifest, type ScoreState, type Task,
  anonymize, formatTime, scoreKey,
} from '@/lib/evaluation';

type ModelTool = {
  name: string;
  title?: string;
  description: string;
  inputSchema: Record<string, unknown>;
  annotations?: { readOnlyHint?: boolean; untrustedContentHint?: boolean };
  execute(input: unknown): unknown;
};

declare global {
  interface Document {
    modelContext?: { registerTool(tool: ModelTool, options?: { signal?: AbortSignal }): void | Promise<void> };
  }
}

const scoreGuide = [
  ['5', '非常好', '表现优秀，几乎没有明显问题'],
  ['4', '较好', '少量问题，基本不影响听感'],
  ['3', '一般', '可以接受，但有较明显问题'],
  ['2', '较差', '较多问题，明显影响听感'],
  ['1', '很差', '严重问题，整体难以接受'],
];

const appBasePath = (import.meta.env.BASE_URL ?? '/').replace(/\/$/, '');

function publicPath(path: string) {
  return `${appBasePath}/${path.replace(/^\/+/, '')}`;
}

function downloadFile(name: string, content: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
}

function csvCell(value: unknown) {
  const text = String(value ?? '');
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

type ExportRow = Record<string, string | number>;
type RevealState = Record<string, boolean>;
type PackageFile = { path: string; size_bytes: number; sha256: string };
type LocalPackageTask = { task_type: string; task_title: string; audio_bitrate: string; file_count: number };
type LocalPackageManifest = {
  package_format: 'pop909-local-audio-v1' | 'pop909-local-audio-v2';
  manifest_version: string;
  scope?: 'all_tasks';
  task_type?: string;
  task_title?: string;
  audio_bitrate?: string;
  task_count?: number;
  tasks?: LocalPackageTask[];
  file_count: number;
  files: PackageFile[];
};
type LoadedPackage = { title: string; fileCount: number; bitrate: string };

function selectedPath(file: File) {
  const path = (file.webkitRelativePath || file.name).replaceAll('\\', '/');
  const marker = path.indexOf('/audio/');
  return marker >= 0 ? path.slice(marker + 1) : path.replace(/^\/+/, '');
}

async function fileSha256(file: File) {
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function rowsToCsv(rows: ExportRow[]) {
  const headers = Object.keys(rows[0] ?? {});
  return [headers.join(','), ...rows.map((row) => headers.map((header) => csvCell(row[header])).join(','))].join('\n');
}

function summarizeByModel(manifest: Manifest, rows: ExportRow[], evaluatorId: string, generatedAt: string) {
  const metricIds = [...new Set(manifest.tasks.flatMap((item) => item.metrics.map((metric) => metric.id)))];
  return manifest.tasks.flatMap((item) => {
    const models = [...new Set(item.groups.flatMap((entry) => entry.samples.map((sample) => sample.model)))];
    return models.map((model) => {
      const modelRows = rows.filter((row) => row.task_type === item.task_type && row.real_model_name === model);
      const averages = Object.fromEntries(metricIds.map((metricId) => {
        const values = modelRows.map((row) => row[metricId]).filter((value): value is number => typeof value === 'number');
        return [`${metricId}_mean`, values.length ? Number((values.reduce((sum, value) => sum + value, 0) / values.length).toFixed(4)) : ''];
      }));
      const allMetricValues = modelRows.flatMap((row) => item.metrics.map((metric) => row[metric.id])).filter((value): value is number => typeof value === 'number');
      return {
        evaluator_id: evaluatorId,
        task_type: item.task_type,
        task_title: item.title,
        real_model_name: model,
        sample_count: modelRows.length,
        ...averages,
        all_metrics_mean: Number((allMetricValues.reduce((sum, value) => sum + value, 0) / allMetricValues.length).toFixed(4)),
        generated_at: generatedAt,
        manifest_version: manifest.version,
      } satisfies ExportRow;
    });
  });
}

function AudioPlayer({ sample, source, audioKey, activeAudio, onActivate }: {
  sample: AnonymousSample;
  source?: string;
  audioKey: string;
  activeAudio: string | null;
  onActivate: (key: string) => void;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setFailed(false);
    setTime(0);
    setDuration(0);
  }, [source]);

  useEffect(() => {
    if (activeAudio !== audioKey && audioRef.current && !audioRef.current.paused) {
      audioRef.current.pause();
    }
  }, [activeAudio, audioKey]);

  const toggle = async () => {
    const audio = audioRef.current;
    if (!audio || failed) return;
    if (audio.paused) {
      onActivate(audioKey);
      try { await audio.play(); } catch { setFailed(true); }
    } else {
      audio.pause();
    }
  };

  const replay = async () => {
    const audio = audioRef.current;
    if (!audio || failed) return;
    audio.currentTime = 0;
    onActivate(audioKey);
    try { await audio.play(); } catch { setFailed(true); }
  };

  return (
    <div className="audio-shell">
      <audio
        ref={audioRef}
        src={source}
        preload="metadata"
        onPlay={() => { setPlaying(true); onActivate(audioKey); }}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onTimeUpdate={(event) => setTime(event.currentTarget.currentTime)}
        onLoadedMetadata={(event) => setDuration(event.currentTarget.duration)}
        onError={() => setFailed(true)}
      />
      <Button size="icon-lg" className="rounded-full" onClick={toggle} aria-label={playing ? '暂停' : '播放'} disabled={!source || failed}>
        {playing ? <Pause /> : <Play className="ml-0.5" />}
      </Button>
      <Button size="icon-sm" variant="ghost" onClick={replay} aria-label="重新播放" disabled={!source || failed}><RotateCcw /></Button>
      <input
        className="audio-range"
        type="range"
        min="0"
        max={duration || 1}
        step="0.01"
        value={Math.min(time, duration || 1)}
        aria-label={`${sample.anonymousId} 播放进度`}
        disabled={!source || failed}
        onChange={(event) => {
          if (!audioRef.current) return;
          audioRef.current.currentTime = Number(event.target.value);
          setTime(Number(event.target.value));
        }}
      />
      <span className="min-w-[78px] text-right font-mono text-xs text-muted-foreground">{formatTime(time)} / {formatTime(duration)}</span>
      {!source && <span className="text-xs text-amber-700">请先加载当前任务的本地音频包</span>}
      {source && failed && <span className="text-xs text-destructive">音频无法加载</span>}
    </div>
  );
}

function RulesDialog({ task }: { task: Task }) {
  const [content, setContent] = useState('正在读取评分规则…');
  useEffect(() => {
    let active = true;
    fetch(publicPath(task.rules_markdown))
      .then((response) => response.ok ? response.text() : Promise.reject(new Error('load failed')))
      .then((text) => { if (active) setContent(text); })
      .catch(() => { if (active) setContent('评分规则文件未能加载，请联系实验管理员。'); });
    return () => { active = false; };
  }, [task.rules_markdown]);

  return (
    <Dialog>
      <DialogTrigger render={<Button variant="outline" size="sm" />}><Info data-icon="inline-start" />实验说明与评分规则</DialogTrigger>
      <DialogContent className="max-h-[80vh] max-w-2xl overflow-y-auto p-6">
        <DialogHeader><DialogTitle className="text-xl">{task.title}评分规则</DialogTitle><DialogDescription>以下内容直接读取自配置中指定的 Markdown 文件。</DialogDescription></DialogHeader>
        <div className="rules-copy">
          {content.split(/\r?\n/).filter(Boolean).map((line, index) => {
            if (line.startsWith('# ')) return <h2 key={index}>{line.slice(2)}</h2>;
            if (line.startsWith('## ')) return <h3 key={index}>{line.slice(3)}</h3>;
            if (line.startsWith('- ')) return <p key={index} className="rule-bullet">{line.slice(2)}</p>;
            return <p key={index}>{line}</p>;
          })}
        </div>
      </DialogContent>
    </Dialog>
  );
}

export default function Home() {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [loadError, setLoadError] = useState('');
  const [evaluatorId, setEvaluatorId] = useState('E001');
  const [taskIndex, setTaskIndex] = useState(0);
  const [groupIndices, setGroupIndices] = useState<Record<string, number>>({});
  const [scores, setScores] = useState<ScoreState>({});
  const [revealedModels, setRevealedModels] = useState<RevealState>({});
  const [localAudioSources, setLocalAudioSources] = useState<Record<string, string>>({});
  const [loadedPackages, setLoadedPackages] = useState<Record<string, LoadedPackage>>({});
  const [packageMessage, setPackageMessage] = useState('请选择“全部任务”总包解压后的文件夹。');
  const [checkingPackage, setCheckingPackage] = useState(false);
  const [activeAudio, setActiveAudio] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ title: string; detail: string; kind: 'success' | 'warning' } | null>(null);
  const [hydrated, setHydrated] = useState(false);
  const skipNextSave = useRef(false);
  const skipNextRevealSave = useRef(false);
  const audioInputRef = useRef<HTMLInputElement>(null);
  const objectUrlsRef = useRef<Record<string, string>>({});

  useEffect(() => () => {
    Object.values(objectUrlsRef.current).forEach((url) => URL.revokeObjectURL(url));
  }, []);

  useEffect(() => {
    fetch(publicPath('/data/evaluation_manifest.json'), { cache: 'no-store' })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('manifest missing')))
      .then((data) => setManifest(data as Manifest))
      .catch(() => setLoadError('无法读取 evaluation_manifest.json，请检查 public/data 目录。'));
  }, []);

  useEffect(() => {
    if (!manifest) return;
    skipNextSave.current = true;
    skipNextRevealSave.current = true;
    const storageKey = `pop909:${manifest.study_id}:${manifest.version}:${evaluatorId.trim().toUpperCase()}`;
    const saved = localStorage.getItem(storageKey);
    const savedReveals = localStorage.getItem(`${storageKey}:revealed-models`);
    try { setScores(saved ? JSON.parse(saved) : {}); } catch { setScores({}); }
    try { setRevealedModels(savedReveals ? JSON.parse(savedReveals) : {}); } catch { setRevealedModels({}); }
    setHydrated(true);
  }, [manifest, evaluatorId]);

  useEffect(() => {
    if (!manifest || !hydrated) return;
    if (skipNextSave.current) { skipNextSave.current = false; return; }
    localStorage.setItem(`pop909:${manifest.study_id}:${manifest.version}:${evaluatorId.trim().toUpperCase()}`, JSON.stringify(scores));
  }, [scores, manifest, evaluatorId, hydrated]);

  useEffect(() => {
    if (!manifest || !hydrated) return;
    if (skipNextRevealSave.current) { skipNextRevealSave.current = false; return; }
    const storageKey = `pop909:${manifest.study_id}:${manifest.version}:${evaluatorId.trim().toUpperCase()}`;
    localStorage.setItem(`${storageKey}:revealed-models`, JSON.stringify(revealedModels));
  }, [revealedModels, manifest, evaluatorId, hydrated]);

  const task = manifest?.tasks[taskIndex];
  const groupIndex = task ? (groupIndices[task.task_type] ?? 0) : 0;
  const group = task?.groups[groupIndex];
  const samplingGroups = useMemo(() => {
    if (!task) return [];
    if (task.sampling_groups?.length) return task.sampling_groups;
    return [{ id: 'primary', title: '初始抽样', sample_count: task.groups.length }];
  }, [task]);
  const activeSamplingGroupId = group?.sampling_group_id ?? 'primary';
  const activeSamplingGroup = samplingGroups.find((entry) => entry.id === activeSamplingGroupId) ?? samplingGroups[0];
  const activeSamplingGroupIndices = useMemo(() => task
    ? task.groups.map((entry, index) => ({ entry, index })).filter(({ entry }) => (entry.sampling_group_id ?? 'primary') === activeSamplingGroupId)
    : [], [activeSamplingGroupId, task]);
  const activeSamplingGroupPosition = Math.max(0, activeSamplingGroupIndices.findIndex((entry) => entry.index === groupIndex));
  const samples = useMemo(() => group ? anonymize(group, evaluatorId, manifest?.show_model_names) : [], [group, evaluatorId, manifest?.show_model_names]);

  const loadLocalPackage = async (fileList: FileList | null) => {
    if (!manifest || !fileList?.length) return;
    setCheckingPackage(true);
    setPackageMessage('正在核对数据包版本与文件完整性…');
    try {
      const selectedFiles = Array.from(fileList);
      const packageFiles = selectedFiles.filter((file) => /(^|\/)package_manifest\.json$/i.test(file.webkitRelativePath || file.name));
      if (packageFiles.length !== 1) throw new Error('请选择一个完整解压的数据包文件夹。');
      const packageManifest = JSON.parse(await packageFiles[0].text()) as LocalPackageManifest;
      if (!['pop909-local-audio-v1', 'pop909-local-audio-v2'].includes(packageManifest.package_format)) throw new Error('无法识别这个数据包格式。');
      if (packageManifest.manifest_version !== manifest.version) throw new Error(`数据包版本不匹配：需要 ${manifest.version}。`);
      const packageTasks: LocalPackageTask[] = packageManifest.package_format === 'pop909-local-audio-v2'
        ? packageManifest.tasks ?? []
        : [{
            task_type: packageManifest.task_type ?? '',
            task_title: packageManifest.task_title ?? '',
            audio_bitrate: packageManifest.audio_bitrate ?? '',
            file_count: packageManifest.file_count,
          }];
      if (!packageTasks.length) throw new Error('数据包没有登记任何评测任务。');
      const targetTasks = packageTasks.map((packageTask) => {
        const targetTask = manifest.tasks.find((item) => item.task_type === packageTask.task_type);
        if (!targetTask) throw new Error(`数据包中的任务不在当前评测版本中：${packageTask.task_type}`);
        const taskPaths = new Set(targetTask.groups.flatMap((entry) => [
          ...(entry.reference ? [entry.reference.audio_url.replace(/^\/+/, '')] : []),
          ...entry.samples.map((sample) => sample.audio_url.replace(/^\/+/, '')),
        ]));
        if (packageTask.file_count !== taskPaths.size) throw new Error(`“${targetTask.title}”文件数量不正确：需要 ${taskPaths.size} 个 MP3。`);
        return { packageTask, targetTask, taskPaths };
      });
      if (packageManifest.package_format === 'pop909-local-audio-v2') {
        const taskTypes = new Set(packageTasks.map((item) => item.task_type));
        if (packageManifest.scope !== 'all_tasks' || packageManifest.task_count !== manifest.tasks.length || taskTypes.size !== manifest.tasks.length || manifest.tasks.some((item) => !taskTypes.has(item.task_type))) {
          throw new Error('这个总包没有完整包含当前版本的所有评测任务。');
        }
      }
      const expectedPaths = new Set(targetTasks.flatMap(({ taskPaths }) => [...taskPaths]));
      if (packageManifest.file_count !== packageManifest.files.length || packageManifest.files.length !== expectedPaths.size) {
        throw new Error(`文件数量不正确：需要 ${expectedPaths.size} 个 MP3。`);
      }
      const selectedByPath = new Map(
        selectedFiles.filter((file) => file.name.toLowerCase().endsWith('.mp3')).map((file) => [selectedPath(file), file]),
      );
      const validatedFiles: Array<[string, File]> = [];
      for (const item of packageManifest.files) {
        const path = item.path.replace(/^\/+/, '');
        if (!expectedPaths.has(path)) throw new Error(`数据包包含当前任务未登记的文件：${path}`);
        const file = selectedByPath.get(path);
        if (!file) throw new Error(`缺少音频文件：${path}`);
        if (file.size !== item.size_bytes) throw new Error(`文件大小校验失败：${path}`);
        if (await fileSha256(file) !== item.sha256.toLowerCase()) throw new Error(`文件校验失败：${path}`);
        validatedFiles.push([`/${path}`, file]);
      }
      const nextSources = Object.fromEntries(validatedFiles.map(([path, file]) => [path, URL.createObjectURL(file)]));

      setLocalAudioSources((current) => {
        const next = { ...current };
        Object.keys(nextSources).forEach((path) => {
          const previous = objectUrlsRef.current[path];
          if (previous) URL.revokeObjectURL(previous);
          next[path] = nextSources[path];
          objectUrlsRef.current[path] = nextSources[path];
        });
        return next;
      });
      setLoadedPackages((current) => ({
        ...current,
        ...Object.fromEntries(targetTasks.map(({ packageTask, targetTask }) => [targetTask.task_type, {
          title: packageTask.task_title,
          fileCount: packageTask.file_count,
          bitrate: packageTask.audio_bitrate,
        }])),
      }));
      if (targetTasks.length === 1) {
        const targetIndex = manifest.tasks.findIndex((item) => item.task_type === targetTasks[0].targetTask.task_type);
        setTaskIndex(targetIndex);
      }
      setActiveAudio(null);
      setPackageMessage(targetTasks.length === manifest.tasks.length
        ? `已一次加载全部 ${targetTasks.length} 个任务，共 ${packageManifest.file_count} 个本地音频。`
        : `已验证「${targetTasks[0].targetTask.title}」：${packageManifest.file_count} 个本地音频。`);
    } catch (error) {
      setPackageMessage(error instanceof Error ? error.message : '数据包读取失败。');
    } finally {
      setCheckingPackage(false);
      if (audioInputRef.current) audioInputRef.current.value = '';
    }
  };

  const isSampleComplete = useCallback((currentTask: Task, groupId: string, anonymousId: string) => {
    const record = scores[scoreKey(currentTask.task_type, groupId, anonymousId)] ?? {};
    return currentTask.metrics.every((metric) => Number.isInteger(record[metric.id]));
  }, [scores]);

  const isGroupComplete = useCallback((currentTask: Task, targetIndex: number) => {
    const target = currentTask.groups[targetIndex];
    return anonymize(target, evaluatorId, manifest?.show_model_names).every((sample) => isSampleComplete(currentTask, target.group_id, sample.anonymousId));
  }, [evaluatorId, isSampleComplete, manifest?.show_model_names]);

  const totalGroups = manifest?.tasks.reduce((sum, item) => sum + item.groups.length, 0) ?? 0;
  const completedGroups = manifest?.tasks.reduce((sum, item) => sum + item.groups.filter((_, index) => isGroupComplete(item, index)).length, 0) ?? 0;
  const progress = totalGroups ? Math.round((completedGroups / totalGroups) * 100) : 0;

  const buildExport = useCallback((format: 'csv' | 'json', report: 'detail' | 'summary' = 'detail', scopeTaskType?: string, triggerDownload = true) => {
    if (!manifest) throw new Error('评测配置尚未加载。');
    if (!evaluatorId.trim()) throw new Error('请填写 evaluator ID。');
    const selectedTasks = scopeTaskType
      ? manifest.tasks.filter((item) => item.task_type === scopeTaskType)
      : manifest.tasks;
    if (!selectedTasks.length) throw new Error('未找到要导出的评测任务。');
    const scopeLabel = scopeTaskType ? selectedTasks[0].title : '全部任务';
    const generatedAt = new Date().toISOString();
    const rows = selectedTasks.flatMap((item) => item.groups.flatMap((entry) =>
      anonymize(entry, evaluatorId, manifest.show_model_names).map((sample) => ({
        evaluator_id: evaluatorId.trim(), task_type: item.task_type, group_id: entry.group_id,
        sampling_group_id: entry.sampling_group_id ?? 'primary',
        sampling_group_title: entry.sampling_group_title ?? '初始抽样',
        sample_id: entry.sample_id, anonymous_sample_id: sample.anonymousId,
        real_model_name: sample.model, display_order: sample.order,
        ...Object.fromEntries(item.metrics.map((metric) => [metric.id, scores[scoreKey(item.task_type, entry.group_id, sample.anonymousId)]?.[metric.id] ?? ''])),
        timestamp: generatedAt, manifest_version: manifest.version,
      }))
    )) as ExportRow[];
    const missing = rows.filter((row) => Object.values(row).includes('')).length;
    if (missing) throw new Error(`${scopeLabel}还有 ${missing} 个 Sample 未完成全部评分。`);
    const summary = summarizeByModel({ ...manifest, tasks: selectedTasks }, rows, evaluatorId.trim(), generatedAt);
    const stamp = new Date().toISOString().slice(0, 10);
    const scopeSlug = scopeTaskType ?? 'all_tasks';
    if (format === 'json') {
      const content = JSON.stringify({ study_id: manifest.study_id, evaluator_id: evaluatorId.trim(), task_scope: selectedTasks.map((item) => ({ task_type: item.task_type, title: item.title })), exported_at: generatedAt, rows, model_averages: summary }, null, 2);
      if (triggerDownload) downloadFile(`pop909_pop1k7_${scopeSlug}_${evaluatorId}_${stamp}.json`, content, 'application/json');
      return { format, report: 'complete', records: rows.length, summaryRecords: summary.length, scopeLabel };
    }
    const selectedRows = report === 'summary' ? summary : rows;
    if (triggerDownload) {
      const suffix = report === 'summary' ? 'model_averages' : 'detail';
      downloadFile(`pop909_pop1k7_${scopeSlug}_${evaluatorId}_${suffix}_${stamp}.csv`, `\ufeff${rowsToCsv(selectedRows)}`, 'text/csv;charset=utf-8');
    }
    return { format, report, records: selectedRows.length, summaryRecords: summary.length, scopeLabel };
  }, [evaluatorId, manifest, scores]);

  useEffect(() => {
    const context = document.modelContext;
    if (!context?.registerTool || !manifest) return;
    const lifecycle = new AbortController();
    const register = (tool: ModelTool) => Promise.resolve(context.registerTool(tool, { signal: lifecycle.signal })).catch(() => undefined);
    void register({
      name: 'read_evaluation_progress', title: '读取评测进度',
      description: '读取当前 POP909 与 Pop1k7 评测的已完成组数、总组数和完成百分比。',
      inputSchema: { type: 'object', properties: {}, additionalProperties: false },
      annotations: { readOnlyHint: true, untrustedContentHint: false },
      execute: () => ({ evaluator_id: evaluatorId, completed_groups: completedGroups, total_groups: totalGroups, percent: progress }),
    });
    void register({
      name: 'export_evaluation_results', title: '导出评测结果',
      description: '导出单个已完成任务或全部任务的逐条评分、模型平均分，或包含二者的 JSON。',
      inputSchema: { type: 'object', properties: { format: { type: 'string', enum: ['csv', 'json'] }, report: { type: 'string', enum: ['detail', 'summary'] }, task_type: { type: 'string', enum: manifest.tasks.map((item) => item.task_type), description: '省略时导出全部任务。' } }, required: ['format'], additionalProperties: false },
      annotations: { readOnlyHint: false, untrustedContentHint: false },
      execute: (input) => {
        const value = input as { format?: string; report?: string; task_type?: string };
        if (value.format !== 'csv' && value.format !== 'json') throw new Error('format 必须为 csv 或 json。');
        if (value.task_type && !manifest.tasks.some((item) => item.task_type === value.task_type)) throw new Error('task_type 不存在。');
        const report = value.report === 'summary' ? 'summary' : 'detail';
        return buildExport(value.format, report, value.task_type);
      },
    });
    return () => lifecycle.abort();
  }, [buildExport, completedGroups, evaluatorId, manifest, progress, totalGroups]);

  if (loadError) return <main className="grid min-h-screen place-items-center p-6"><div className="max-w-md rounded-2xl border border-destructive/30 bg-card p-6"><CircleAlert className="mb-3 text-destructive" /><h1 className="text-lg font-semibold">评测配置加载失败</h1><p className="mt-2 text-sm text-muted-foreground">{loadError}</p></div></main>;
  if (!manifest || !task || !group) return <main className="grid min-h-screen place-items-center text-sm text-muted-foreground">正在加载评测配置…</main>;

  const goToGroup = (nextIndex: number) => {
    if (nextIndex > groupIndex && !isGroupComplete(task, groupIndex)) {
      setNotice({ title: '当前组尚未完成', detail: '请为每个 Sample 的所有指标评分后再继续。', kind: 'warning' });
      return;
    }
    setActiveAudio(null);
    setGroupIndices((current) => ({ ...current, [task.task_type]: Math.max(0, Math.min(nextIndex, task.groups.length - 1)) }));
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const goToSamplingGroup = (samplingGroupId: string) => {
    const nextIndex = task.groups.findIndex((entry) => (entry.sampling_group_id ?? 'primary') === samplingGroupId);
    if (nextIndex < 0) return;
    setActiveAudio(null);
    setGroupIndices((current) => ({ ...current, [task.task_type]: nextIndex }));
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const exportWithNotice = (format: 'csv' | 'json', report: 'detail' | 'summary' = 'detail', scopeTaskType?: string) => {
    try {
      const result = buildExport(format, report, scopeTaskType);
      const detail = format === 'json'
        ? `已导出${result.scopeLabel}的 ${result.records} 条评分明细和 ${result.summaryRecords} 条模型平均分。`
        : `已导出${result.scopeLabel}的 ${result.records} 条${report === 'summary' ? '模型平均分' : '评分明细'}。`;
      setNotice({ title: '导出完成', detail, kind: 'success' });
    } catch (error) {
      setNotice({ title: '暂时不能导出', detail: error instanceof Error ? error.message : '请检查评分完整性。', kind: 'warning' });
    }
  };

  return (
    <main className="min-h-screen bg-background text-foreground">
        <header className="sticky top-0 z-30 border-b border-border bg-card/92 backdrop-blur-xl">
          <div className="mx-auto flex max-w-[1500px] items-center justify-between gap-4 px-4 py-3 sm:px-6 lg:px-8">
            <div className="flex items-center gap-3">
              <span className="grid size-10 place-items-center rounded-xl bg-primary text-primary-foreground"><Music2 className="size-5" /></span>
              <div><p className="text-[.7rem] font-bold uppercase tracking-[.18em] text-muted-foreground">POP909 + POP1K7 Study</p><h1 className="text-base font-semibold tracking-tight sm:text-lg">音乐生成主观评测</h1></div>
            </div>
            <div className="flex items-center gap-2">
              <div className="hidden items-center gap-2 text-xs text-muted-foreground md:flex"><Save className="size-3.5 text-primary" />本机自动保存</div>
              <span className="hidden h-5 w-px bg-border md:block" />
              <label className="flex items-center gap-2 text-xs text-muted-foreground">评测员<Input aria-label="Evaluator ID" value={evaluatorId} onChange={(event) => setEvaluatorId(event.target.value)} className="h-8 w-24 bg-background font-mono text-xs uppercase" /></label>
            </div>
          </div>
        </header>

        <div className="mx-auto grid max-w-[1500px] gap-6 px-4 py-6 sm:px-6 lg:grid-cols-[230px_minmax(0,1fr)_250px] lg:px-8">
          <aside className="min-w-0 space-y-5">
            <section>
              <p className="eyebrow">评测任务</p>
              <Tabs value={task.task_type} onValueChange={(value) => { const index = manifest.tasks.findIndex((item) => item.task_type === value); if (index >= 0) { setTaskIndex(index); setActiveAudio(null); } }} className="mt-3">
                <TabsList className="grid !h-auto w-full grid-cols-1 gap-2 bg-transparent p-0" style={{ height: 'auto' }}>
                  {manifest.tasks.map((item, index) => (
                    <TabsTrigger key={item.task_type} value={item.task_type} className="task-tab"><span className="shrink-0 font-mono text-xs">0{index + 1}</span><span className="min-w-0 whitespace-normal break-words leading-5">{item.title}</span><small className="shrink-0">{item.groups.filter((_, i) => isGroupComplete(item, i)).length}/{item.groups.length}</small></TabsTrigger>
                  ))}
                </TabsList>
              </Tabs>
            </section>
            <section className="rounded-2xl border border-primary/25 bg-card p-4">
              <input
                ref={audioInputRef}
                type="file"
                multiple
                accept="audio/mpeg,.mp3,application/json"
                className="hidden"
                {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
                onChange={(event) => void loadLocalPackage(event.currentTarget.files)}
              />
              <p className="eyebrow">本地音频总包</p>
              <p className={`mt-2 text-xs leading-5 ${packageMessage.includes('失败') || packageMessage.includes('不匹配') || packageMessage.includes('缺少') ? 'text-destructive' : 'text-muted-foreground'}`}>{packageMessage}</p>
              <Button className="mt-3 w-full" variant="outline" disabled={checkingPackage} onClick={() => audioInputRef.current?.click()}>
                {checkingPackage ? <LoaderCircle className="animate-spin" data-icon="inline-start" /> : <FolderOpen data-icon="inline-start" />}
                {checkingPackage ? '正在验证…' : '打开任务文件夹'}
              </Button>
              <div className="mt-3 space-y-2">
                {manifest.tasks.map((item) => {
                  const loaded = loadedPackages[item.task_type];
                  return <div key={item.task_type} className="flex items-center gap-2 text-xs">
                    {loaded ? <PackageCheck className="size-3.5 shrink-0 text-primary" /> : <span className="ml-1 size-1.5 shrink-0 rounded-full bg-border" />}
                    <span className={loaded ? 'text-foreground' : 'text-muted-foreground'}>{item.title}</span>
                    {loaded && <span className="ml-auto font-mono text-[.68rem] text-muted-foreground">{loaded.bitrate}</span>}
                  </div>;
                })}
              </div>
              <p className="mt-3 border-t border-border pt-3 text-xs leading-5 text-muted-foreground">请选择“全部任务”总包解压后的文件夹。文件只在本机读取，不会上传；重新打开浏览器后需要再次选择。</p>
            </section>
            <section className="rounded-2xl border border-border bg-card p-4">
              <div className="mb-3 flex items-center justify-between"><span className="text-sm font-semibold">总体进度</span><span className="font-mono text-xs text-muted-foreground">{completedGroups} / {totalGroups}</span></div>
              <Progress value={progress} className="[&_[data-slot=progress-indicator]]:bg-primary [&_[data-slot=progress-track]]:h-2"><ProgressLabel className="sr-only">总体进度</ProgressLabel><ProgressValue className="sr-only">{() => `${progress}%`}</ProgressValue></Progress>
              <p className="mt-3 text-xs leading-5 text-muted-foreground">评分保存在当前浏览器中。更换设备前请导出结果。</p>
            </section>
            <div className="flex items-start gap-3 border-l-2 border-primary/40 pl-4 text-sm leading-6 text-muted-foreground"><ShieldCheck className="mt-1 size-4 shrink-0 text-primary" />{manifest.show_model_names ? '调试模式：当前直接显示模型名称。' : manifest.allow_model_reveal_after_scoring ? '模型顺序随机；完成单个 Sample 的全部评分后，可选择查看真实模型。' : '模型身份与文件名已匿名，请仅依据听感评分。'}</div>
            {manifest.phase === 'development' && <div className="rounded-xl border border-amber-300/60 bg-amber-50 p-3 text-xs leading-5 text-amber-900"><strong>检查阶段</strong><br />已接入真实评测音频；抽样清单尚未冻结。</div>}
            {manifest.quality_notice && <p className="rounded-xl border border-amber-300/60 bg-amber-50 p-3 text-xs leading-5 text-amber-900">{manifest.quality_notice}</p>}
          </aside>

          <section className="min-w-0">
            <div className="mb-5 flex flex-wrap items-end justify-between gap-4 border-b border-border pb-5">
              <div><p className="eyebrow">{task.title} · {activeSamplingGroup?.title ?? '初始抽样'} · Group {String(activeSamplingGroupPosition + 1).padStart(2, '0')}</p><h2 className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">比较本组完整生成结果</h2><p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">{task.short_description} 同一时间只会播放一个 Sample。</p></div>
              <div className="flex flex-wrap items-center justify-end gap-2">
                {samplingGroups.length > 1 && <Select value={activeSamplingGroupId} onValueChange={(value) => value && goToSamplingGroup(value)}>
                  <SelectTrigger className="h-9 min-w-40 bg-card" aria-label="选择抽样组"><SelectValue /></SelectTrigger>
                  <SelectContent>{samplingGroups.map((entry) => <SelectItem key={entry.id} value={entry.id}>{entry.title}（{entry.sample_count} 首）</SelectItem>)}</SelectContent>
                </Select>}
                <RulesDialog task={task} /><span className="rounded-full border border-border bg-card px-3 py-1.5 font-mono text-xs text-muted-foreground">{activeSamplingGroupPosition + 1} / {activeSamplingGroupIndices.length}</span>
              </div>
            </div>

            <div className="space-y-5">
              {group.reference && (
                <article className="sample-card border-primary/40" key={`reference:${group.group_id}`}>
                  <div className="sample-head">
                    <div><span className="sample-index"><Music2 className="size-5" /></span><div><h3 className="text-lg font-semibold">GT · 原曲参考</h3><p className="text-xs text-muted-foreground">建议先试听原曲，再评价下方生成结果；参考音频无需评分。</p></div></div>
                    <span className="status-pill"><Headphones />优先试听</span>
                  </div>
                  <div className="border-t border-border bg-secondary/30 px-4 py-3 sm:px-6">
                    <AudioPlayer sample={{ ...group.reference, anonymousId: 'GT 原曲参考', order: 0 }} source={localAudioSources[group.reference.audio_url]} audioKey={`${task.task_type}:${group.group_id}:reference`} activeAudio={activeAudio} onActivate={setActiveAudio} />
                  </div>
                </article>
              )}
              {samples.map((sample) => {
                const key = scoreKey(task.task_type, group.group_id, sample.anonymousId);
                const record = scores[key] ?? {};
                const complete = isSampleComplete(task, group.group_id, sample.anonymousId);
                const revealed = Boolean(revealedModels[key]);
                return (
                  <article className="sample-card" key={`${group.group_id}:${sample.audio_url}`}>
                    <div className="sample-head">
                      <div><span className="sample-index">{manifest.show_model_names ? sample.order : sample.anonymousId.replace('Sample ', '')}</span><div><h3 className="text-lg font-semibold">{sample.anonymousId}</h3><p className="text-xs text-muted-foreground">{revealed ? `真实模型：${sample.model}` : '完整音频 · 可重复试听'}</p></div></div>
                      <div className="flex flex-wrap items-center justify-end gap-2">
                        <span className={`status-pill ${complete ? 'status-done' : ''}`}>{complete ? <Check /> : <Headphones />}{complete ? '已完成评分' : '等待评分'}</span>
                        {manifest.allow_model_reveal_after_scoring && !manifest.show_model_names && (
                          <Button
                            size="sm"
                            variant={revealed ? 'secondary' : 'outline'}
                            disabled={!complete || revealed}
                            onClick={() => setRevealedModels((current) => ({ ...current, [key]: true }))}
                          >
                            {revealed ? `真实模型：${sample.model}` : complete ? '查看真实模型' : '评分完成后可查看'}
                          </Button>
                        )}
                      </div>
                    </div>
                    <div className="border-y border-border bg-secondary/30 px-4 py-3 sm:px-6"><AudioPlayer sample={sample} source={localAudioSources[sample.audio_url]} audioKey={key} activeAudio={activeAudio} onActivate={setActiveAudio} /></div>
                    <div className="divide-y divide-border">
                      {task.metrics.map((metric) => (
                        <fieldset key={metric.id} className="metric-row">
                          <legend className="sr-only">{metric.label}</legend>
                          <div><p className="font-medium">{metric.label}</p><p className="mt-1 text-sm leading-5 text-muted-foreground">{metric.description}</p></div>
                          <RadioGroup aria-label={`${sample.anonymousId} ${metric.label}评分`} className="score-grid" value={record[metric.id] ? String(record[metric.id]) : ''} onValueChange={(value) => setScores((current) => ({ ...current, [key]: { ...(current[key] ?? {}), [metric.id]: Number(value) } }))}>
                            {[1, 2, 3, 4, 5].map((score) => <label key={score} className="score-choice" title={`${score} 分`}><RadioGroupItem value={String(score)} className="sr-only" /><span>{score}</span></label>)}
                          </RadioGroup>
                        </fieldset>
                      ))}
                    </div>
                  </article>
                );
              })}
            </div>

            <div className="mt-6 flex items-center justify-between border-t border-border pt-5">
              <Button variant="outline" onClick={() => goToGroup(activeSamplingGroupIndices[activeSamplingGroupPosition - 1]?.index ?? groupIndex)} disabled={activeSamplingGroupPosition === 0}><ArrowLeft data-icon="inline-start" />上一组</Button>
              <div className="hidden text-center text-xs text-muted-foreground sm:block">完成当前组全部评分后可继续</div>
              <Button onClick={() => goToGroup(activeSamplingGroupIndices[activeSamplingGroupPosition + 1]?.index ?? groupIndex)} disabled={activeSamplingGroupPosition === activeSamplingGroupIndices.length - 1}>下一组<ArrowRight data-icon="inline-end" /></Button>
            </div>
          </section>

          <aside className="min-w-0">
            <div className="sticky top-24 space-y-4">
              <section className="rounded-2xl border border-border bg-card p-5 shadow-sm">
                <p className="eyebrow">评分参考</p>
                <ol className="mt-4 space-y-3">{scoreGuide.map(([score, label, hint]) => <li key={score} className="flex gap-3 text-sm"><span className="grid size-7 shrink-0 place-items-center rounded-lg bg-secondary font-mono font-bold text-primary">{score}</span><span><strong className="font-medium">{label}</strong><small className="block leading-5 text-muted-foreground">{hint}</small></span></li>)}</ol>
              </section>
              <section className="rounded-2xl border border-border bg-card p-5">
                <p className="eyebrow">导出结果</p>
                <p className="mt-2 text-xs leading-5 text-muted-foreground">当前任务完成后即可单独保存。完整结果同时包含逐条评分和按模型计算的平均分。</p>
                <div className="mt-4 grid gap-2">
                  <Button className="export-button" onClick={() => exportWithNotice('json', 'detail', task.task_type)} disabled={!task.groups.every((_, index) => isGroupComplete(task, index))}><Download data-icon="inline-start" /><span className="min-w-0">导出当前「{task.title}」结果</span></Button>
                  <Button className="export-button" variant="outline" onClick={() => exportWithNotice('json')} disabled={completedGroups !== totalGroups}><FileJson data-icon="inline-start" /><span className="min-w-0">导出所有任务结果</span></Button>
                </div>
                <p className="mt-3 text-xs leading-5 text-muted-foreground">{task.groups.every((_, index) => isGroupComplete(task, index)) ? '当前任务已完成，可以导出。' : `当前任务完成 ${task.groups.filter((_, index) => isGroupComplete(task, index)).length}/${task.groups.length} 组。`}</p>
              </section>
              <nav className="rounded-2xl border border-border bg-card p-3" aria-label="组导航">
                <p className="px-2 pb-2 text-xs font-medium text-muted-foreground">{task.title}分组</p>
                {samplingGroups.map((samplingGroup) => {
                  const entries = task.groups.map((entry, index) => ({ entry, index })).filter(({ entry }) => (entry.sampling_group_id ?? 'primary') === samplingGroup.id);
                  return <div key={samplingGroup.id} className="mb-3 last:mb-0">
                    {samplingGroups.length > 1 && <p className="px-2 py-1 text-[.68rem] font-semibold uppercase tracking-wider text-muted-foreground">{samplingGroup.title}</p>}
                    {entries.map(({ entry, index }, position) => <button key={entry.group_id} type="button" onClick={() => goToGroup(index)} className={`group-link ${index === groupIndex ? 'group-link-active' : ''}`}><span>Group {String(position + 1).padStart(2, '0')}</span>{isGroupComplete(task, index) ? <Check /> : <ChevronRight />}</button>)}
                  </div>;
                })}
              </nav>
            </div>
          </aside>
        </div>
      {notice && <output className={`notice ${notice.kind === 'success' ? 'notice-success' : ''}`} aria-live="polite"><div><strong>{notice.title}</strong><span>{notice.detail}</span></div><button type="button" onClick={() => setNotice(null)} aria-label="关闭提示">×</button></output>}
    </main>
  );
}
