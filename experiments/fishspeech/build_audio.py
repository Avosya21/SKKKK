"""Собирает озвучку ролика из лучших вариантов: audio.wav (48 кГц) + timeline.ts для Remotion.
python build_audio.py [--speed 1.0]"""
import json, os, subprocess, sys, wave
import numpy as np
sys.stdout.reconfigure(encoding='utf-8')
HERE = os.path.dirname(os.path.abspath(__file__))
PROMO = os.path.join(HERE, '..', 'promo_video')
FPS, SR = 30, 48000
speed = float(sys.argv[sys.argv.index('--speed') + 1]) if '--speed' in sys.argv else 1.0
LEAD, GAP, SCENE_GAP, TAIL = 6, 6, 12, 24
plan = json.load(open(os.path.join(HERE, 'out', 'plan.json'), encoding='utf-8'))

def load(path):
    af = ['aresample=%d' % SR] if abs(speed - 1) < 1e-6 else ['atempo=%.4f' % speed, 'aresample=%d' % SR]
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-af', ','.join(af), '-ac', '1', '-f', 'f32le', '-'], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()

t = LEAD / FPS; parts = []; subs = []; starts = {}; prev_sc = None
for p in plan:
    if prev_sc is not None: t += (SCENE_GAP if p['scene'] != prev_sc else GAP) / FPS
    prev_sc = p['scene']
    x = load(p['file']); dur = len(x) / SR
    starts.setdefault(p['scene'], int(round(t * FPS)) + 1)
    cs = [s / speed for s in p['chunk_starts']]
    for k, (c, s) in enumerate(zip(p['chunks'], cs)):
        e = cs[k + 1] if k + 1 < len(cs) else dur
        subs.append({'from': int(round((t + s) * FPS)) + 1, 'to': int(round((t + e) * FPS)), 'text': c})
    parts.append((t, x)); t += dur
total = int(np.ceil((t + TAIL / FPS) * FPS))
y = np.zeros(int(total / FPS * SR) + SR, dtype=np.float32)
for st, x in parts:
    a = int(st * SR); y[a:a + len(x)] += x
y = y[:int(total / FPS * SR)]
y = y / (float(np.max(np.abs(y))) or 1.0) * 0.89
out = os.path.join(PROMO, 'public', 'assets', 'audio.wav')
os.makedirs(os.path.dirname(out), exist_ok=True)
pcm = (np.clip(np.stack([y, y], 1), -1, 1) * 32767).astype(np.int16)
with wave.open(out, 'wb') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())
for i in range(len(subs) - 1):
    if subs[i]['to'] >= subs[i + 1]['from']: subs[i]['to'] = subs[i + 1]['from'] - 1
    if subs[i]['to'] < subs[i]['from']: subs[i]['to'] = subs[i]['from']
with open(os.path.join(PROMO, 'src', 'timeline.ts'), 'w', encoding='utf-8', newline='\n') as f:
    f.write('// auto: experiments/fishspeech/build_audio.py\n')
    f.write('export const TOTAL_FRAMES = %d;\n' % total)
    f.write('export const SCENE_FROM: Record<number, number> = %s;\n' % json.dumps(starts))
    f.write('export const SUBS: Array<{from: number; to: number; text: string}> = %s;\n' % json.dumps(subs, ensure_ascii=False))
print('frames', total, '%.1fs' % (total / FPS), 'speed', speed, 'scenes', starts, 'subs', len(subs))
