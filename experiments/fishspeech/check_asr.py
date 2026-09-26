"""Проверка разборчивости: расшифровать wav локальным Parakeet (venvs/lab) и сравнить с исходным текстом (WER по словам)."""
import sys, re, json, soundfile as sf, onnx_asr, numpy as np
sys.stdout.reconfigure(encoding='utf-8')
m = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", "data/models/onnx-asr/nemo-parakeet-tdt-0.6b-v3", providers=["CPUExecutionProvider"])
norm = lambda s: re.sub(r'[^\w\s]', '', s.lower().replace('ё', 'е')).split()
def wer(ref, hyp):
    r, h = norm(ref), norm(hyp)
    d = np.zeros((len(r) + 1, len(h) + 1), int); d[:, 0] = range(len(r) + 1); d[0, :] = range(len(h) + 1)
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i, j] = min(d[i-1, j] + 1, d[i, j-1] + 1, d[i-1, j-1] + (r[i-1] != h[j-1]))
    return d[-1, -1] / max(1, len(r))
res = {}
for item in json.load(open(sys.argv[1], encoding='utf-8')):
    x, sr = sf.read(item['wav'], dtype='float32')
    if x.ndim > 1: x = x.mean(1)
    hyp = str(m.recognize(x, sample_rate=sr))
    res[item['wav']] = {'ref': item['text'], 'hyp': hyp, 'wer': round(wer(item['text'], hyp), 3), 'sec': round(len(x) / sr, 1), 'peak': round(float(np.abs(x).max()), 2)}
json.dump(res, open(sys.argv[2], 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('ok')
