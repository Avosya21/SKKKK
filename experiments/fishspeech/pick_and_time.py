"""Запускать в venvs/lab: python experiments/fishspeech/pick_and_time.py
Для каждого предложения: ASR (Parakeet, метки токенов) всех вариантов -> WER -> лучший -> тайминги блоков субтитров."""
import glob, json, os, re, sys, difflib
import numpy as np, soundfile as sf, onnx_asr
os.environ.setdefault('HF_HUB_OFFLINE', '1')
sys.stdout.reconfigure(encoding='utf-8')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from script_text import SENTS
model = onnx_asr.load_model('nemo-parakeet-tdt-0.6b-v3', os.path.join(HERE, '..', '..', 'data', 'models', 'onnx-asr', 'nemo-parakeet-tdt-0.6b-v3'), providers=['CPUExecutionProvider']).with_timestamps()
norm = lambda s: re.sub(r'[^\w\s]', ' ', s.lower().replace('ё', 'е')).split()

def trim(x, sr, thr=0.006):
    idx = np.where(np.abs(x) > thr)[0]
    if not len(idx): return x
    return x[max(0, idx[0] - int(0.05 * sr)):min(len(x), idx[-1] + int(0.15 * sr))]

def asr_words(x, sr):
    r = model.recognize(x, sample_rate=sr)
    toks = list(r.tokens or []); st = [float(t) for t in (r.timestamps or [])]
    words, cur, t0 = [], '', None
    for tk, t in zip(toks, st):
        if (tk.startswith('▁') or tk.startswith(' ')) and cur:
            words.append((cur, t0)); cur, t0 = '', None
        piece = tk.lstrip('▁ ')
        if t0 is None: t0 = t
        cur += piece
    if cur: words.append((cur, t0))
    return str(r.text), [(norm(w)[0] if norm(w) else w, t) for w, t in words if norm(w)]

def wer(ref, hyp):
    r, h = ref, hyp
    d = np.zeros((len(r) + 1, len(h) + 1), int); d[:, 0] = range(len(r) + 1); d[0, :] = range(len(h) + 1)
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i, j] = min(d[i-1, j] + 1, d[i, j-1] + 1, d[i-1, j-1] + (r[i-1] != h[j-1]))
    return d[-1, -1] / max(1, len(r))

os.makedirs(os.path.join(HERE, 'out', 'best'), exist_ok=True)
plan = []
for i, (sc, text, chunks) in enumerate(SENTS):
    tgt = norm(text)
    cands = []
    for f in sorted(glob.glob(os.path.join(HERE, 'out', 'cand', f's{i:02d}_k*.wav'))):
        x, sr = sf.read(f, dtype='float32')
        if x.ndim > 1: x = x.mean(1)
        x = trim(x, sr)
        hyp, words = asr_words(x, sr)
        cands.append({'file': f, 'x': x, 'sr': sr, 'hyp': hyp, 'words': words, 'wer': wer(tgt, norm(hyp)), 'dur': len(x) / sr})
    med = float(np.median([c['dur'] for c in cands]))
    best = min(cands, key=lambda c: (round(c['wer'], 3), abs(c['dur'] - med)))
    # тайминги блоков: выравниваем слова цели со словами ASR
    aw = [w for w, _ in best['words']]; at = [t for _, t in best['words']]
    sm = difflib.SequenceMatcher(None, tgt, aw, autojunk=False)
    mp = {}
    for a, b, n in sm.get_matching_blocks():
        for k in range(n): mp[a + k] = at[b + k]
    starts = []; pos = 0
    for c in chunks:
        wi = pos; pos += len(norm(c))
        if wi in mp: t = mp[wi]
        else:
            prev = max([k for k in mp if k < wi], default=None); nxt = min([k for k in mp if k > wi], default=None)
            if prev is not None and nxt is not None: t = mp[prev] + (mp[nxt] - mp[prev]) * (wi - prev) / (nxt - prev)
            else: t = best['dur'] * wi / max(1, len(tgt))
        starts.append(round(float(t), 3))
    starts[0] = 0.0
    out = os.path.join(HERE, 'out', 'best', f's{i:02d}.wav')
    sf.write(out, best['x'], best['sr'])
    plan.append({'i': i, 'scene': sc, 'text': text, 'chunks': chunks, 'file': out, 'sr': best['sr'], 'dur': round(best['dur'], 3), 'wer': round(best['wer'], 3),
                 'hyp': best['hyp'], 'chunk_starts': starts, 'all_wer': [round(c['wer'], 3) for c in cands]})
    print(i, 'wer', plan[-1]['wer'], 'all', plan[-1]['all_wer'], 'dur', plan[-1]['dur'], flush=True)
json.dump(plan, open(os.path.join(HERE, 'out', 'plan.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('saved plan; mean wer', round(float(np.mean([p['wer'] for p in plan])), 3), 'total speech', round(sum(p['dur'] for p in plan), 1), 's')
