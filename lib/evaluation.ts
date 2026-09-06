export type Metric = { id: string; label: string; description: string };
export type Sample = { model: string; audio_url: string };
export type SamplingGroup = { id: string; title: string; sample_count: number };
export type Group = {
  group_id: string;
  sample_id: string;
  sampling_group_id?: string;
  sampling_group_title?: string;
  reference?: Sample;
  samples: Sample[];
};
export type Task = {
  task_type: string;
  title: string;
  short_description: string;
  rules_markdown: string;
  metrics: Metric[];
  sampling_groups?: SamplingGroup[];
  groups: Group[];
};
export type Manifest = {
  study_id: string;
  phase: 'development' | 'frozen';
  version: string;
  audio_delivery?: 'hosted' | 'local_package';
  show_model_names?: boolean;
  allow_model_reveal_after_scoring?: boolean;
  quality_notice?: string;
  tasks: Task[];
};
export type AnonymousSample = Sample & { anonymousId: string; order: number };
export type ScoreState = Record<string, Record<string, number>>;

export const scoreKey = (task: string, group: string, anonymous: string) =>
  `${task}::${group}::${anonymous}`;

function hash(text: string) {
  let value = 2166136261;
  for (let index = 0; index < text.length; index += 1) {
    value ^= text.charCodeAt(index);
    value = Math.imul(value, 16777619);
  }
  return value >>> 0;
}

function random(seed: number) {
  let value = seed || 1;
  return () => {
    value = (value + 0x6d2b79f5) | 0;
    let t = Math.imul(value ^ (value >>> 15), 1 | value);
    t ^= t + Math.imul(t ^ (t >>> 7), 61 | t);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function shuffle<T>(items: T[], seed: string) {
  const rng = random(hash(seed));
  const shuffled = [...items];
  for (let index = shuffled.length - 1; index > 0; index -= 1) {
    const target = Math.floor(rng() * (index + 1));
    [shuffled[index], shuffled[target]] = [shuffled[target], shuffled[index]];
  }
  return shuffled;
}

export function anonymize(
  group: Group,
  evaluatorId: string,
  showModelNames = false,
  taskType = '',
  taskGroupOrdinal = 0,
): AnonymousSample[] {
  if (showModelNames) {
    return group.samples.map((sample, order) => ({
      ...sample,
      anonymousId: sample.model,
      order: order + 1,
    }));
  }
  const evaluator = evaluatorId.trim().toUpperCase();
  const samples = [...group.samples].sort((left, right) => left.model.localeCompare(right.model));
  const cycleOrder = shuffle(samples, `${evaluator}::${taskType}::first`);
  const firstSample = cycleOrder[taskGroupOrdinal % samples.length];
  const remaining = shuffle(
    samples.filter((sample) => sample.model !== firstSample.model),
    `${evaluator}::${taskType}::${group.group_id}::remaining`,
  );
  return [firstSample, ...remaining].map((sample, order) => ({
    ...sample,
    anonymousId: `Sample ${String.fromCharCode(65 + order)}`,
    order: order + 1,
  }));
}

export function formatTime(seconds: number) {
  if (!Number.isFinite(seconds)) return '0:00';
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
}
