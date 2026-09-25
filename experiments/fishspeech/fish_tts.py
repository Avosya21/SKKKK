"""Клиент локального Fish Speech (S2 Pro, http://127.0.0.1:8080). Образец голоса: ref/refA.wav + текст из ref/ref_text.json.
python fish_tts.py "текст" out.wav [--seed 1]"""
import argparse, base64, json, os, sys, time, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
URL = os.environ.get('FISH_URL', 'http://127.0.0.1:8080/v1/tts')

def synth(text, out, ref='refA', seed=1, temperature=0.7, top_p=0.8, timeout=600):
    ref_text = json.load(open(os.path.join(HERE, 'ref', 'ref_text.json'), encoding='utf-8'))[ref]
    audio = base64.b64encode(open(os.path.join(HERE, 'ref', ref + '.wav'), 'rb').read()).decode()
    body = {'text': text, 'format': 'wav', 'seed': seed, 'temperature': temperature, 'top_p': top_p, 'use_memory_cache': 'on',
            'references': [{'audio': audio, 'text': ref_text}]}
    req = urllib.request.Request(URL, data=json.dumps(body).encode('utf-8'), headers={'Content-Type': 'application/json'})
    t = time.time()
    data = urllib.request.urlopen(req, timeout=timeout).read()
    open(out, 'wb').write(data)
    return time.time() - t

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(); ap.add_argument('text'); ap.add_argument('out'); ap.add_argument('--seed', type=int, default=1)
    a = ap.parse_args()
    print('done in %.1fs -> %s' % (synth(a.text, a.out, seed=a.seed), a.out))
