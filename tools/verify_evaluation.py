"""Verify rendered lengths, original MIDI event preservation, and GT references."""
import argparse
import csv
import json
import wave
from collections import Counter
from pathlib import Path

import mido


def note_events(path, scale=1, limit=None, velocity_override=None):
    midi = mido.MidiFile(path)
    events = Counter()
    for track in midi.tracks:
        tick = 0
        for msg in track:
            tick += msg.time
            scaled = round(tick * scale)
            if msg.type in {'note_on', 'note_off'} and (limit is None or scaled <= limit):
                velocity = velocity_override if velocity_override is not None and msg.type == 'note_on' and msg.velocity > 0 else msg.velocity
                events[(scaled, msg.type, msg.channel, msg.note, velocity)] += 1
    return events


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf8'))
    workspace = Path(config['evaluation_workspace'])
    with (workspace/'reports/preprocessing_report.csv').open(encoding='utf-8-sig') as handle:
        report_rows = list(csv.DictReader(handle))
    errors = []
    warnings = []
    method_velocity_overrides = {
        (task['task_type'], method['name']): method.get('velocity_override')
        for task in config['tasks'] for method in task['methods']
    }
    rows = [
        row for row in report_rows
        if (row['task_type'], row['method']) in method_velocity_overrides
    ]
    ignored_report_rows = len(report_rows) - len(rows)
    for row in rows:
        label = '/'.join(row[x] for x in ('task_type','method','sample_id'))
        if row['status'] != 'ok':
            errors.append(label+': '+row['error_message'])
            continue
        processed = mido.MidiFile(row['processed_midi_path'])
        limit = round(float(row['final_length_bars'])*4*processed.ticks_per_beat)
        velocity_override = method_velocity_overrides.get((row['task_type'], row['method']))
        reported_override = row.get('velocity_override', '')
        if reported_override != ('' if velocity_override is None else str(velocity_override)):
            errors.append(label+': velocity override report mismatch')
        expected = note_events(row['source_path'],float(row['tick_scale_factor']),limit,velocity_override)
        actual = note_events(row['processed_midi_path'])
        if expected != actual:
            errors.append(label+': configured note events changed')
        tempos = {msg.tempo for track in processed.tracks for msg in track if msg.type=='set_tempo'}
        if tempos != {mido.bpm2tempo(config['audio']['target_bpm'])}:
            errors.append(label+': inconsistent tempo')
        with wave.open(row['audio_path'],'rb') as audio:
            seconds = audio.getnframes()/audio.getframerate()
            if abs(seconds-float(row['final_duration_sec'])) > 0.01:
                errors.append(label+f': WAV duration {seconds}')
            if audio.getnchannels()!=2 or audio.getframerate()!=config['audio']['sample_rate']:
                errors.append(label+': format mismatch')
        if row['length_warning']:
            warnings.append({'sample':label,'source_bars':row['source_bars_4_4'],'duration_sec':seconds})
        expected_policy = ('original_source_preserved' if velocity_override is None
                           else f'original_timing_pitch_preserved;velocity_override={velocity_override}')
        if row['prompt_policy'] != expected_policy:
            errors.append(label+': unexpected prompt policy')
    manifest_path = workspace/'web_data/evaluation_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf8'))
    references = 0
    rated = 0
    for task in manifest['tasks']:
        for group in task['groups']:
            reference = group.get('reference')
            if not reference or reference['model']!='GroundTruth':
                errors.append(group['group_id']+': missing GT reference')
            else:
                references += 1
            if any(x['model']=='GroundTruth' for x in group['samples']):
                errors.append(group['group_id']+': GT included in blind scoring')
            rated += len(group['samples'])
            for sample in [reference, *group['samples']]:
                if sample and not (workspace/'web_audio'/sample['audio_url'].removeprefix('/audio/')).is_file():
                    errors.append(group['group_id']+': missing web audio')
    result = {'files':len(rows),'ignored_retired_method_rows':ignored_report_rows,
              'gt_reference_count':references,'rated_samples':rated,
              'configured_note_events_match':not errors,'short_source_warnings':warnings,'errors':errors}
    output = workspace/'reports/verification.json'
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if errors:
        raise SystemExit(1)


if __name__=='__main__':
    main()
