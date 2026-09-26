"""Озвучка всех предложений: N вариантов (seed) на предложение -> out/cand/sXX_kY.wav"""
import os, sys, time
sys.stdout.reconfigure(encoding='utf-8')
from script_text import SENTS
from fish_tts import synth
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3
os.makedirs('out/cand', exist_ok=True)
t0 = time.time()
for i, (sc, text, chunks) in enumerate(SENTS):
    for k in range(N):
        out = f'out/cand/s{i:02d}_k{k}.wav'
        if os.path.exists(out) and os.path.getsize(out) > 1000: continue
        dt = synth(text, out, seed=k + 1)
        print(f's{i:02d} k{k} {dt:.0f}s (total {time.time() - t0:.0f}s)', flush=True)
print('DONE', flush=True)
