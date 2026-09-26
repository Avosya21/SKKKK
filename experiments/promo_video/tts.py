"""Озвучка ролика локальным голосом Windows (Microsoft Irina, SAPI) -> public/assets/audio.wav + src/timeline.ts."""
import json, os, subprocess, wave
import numpy as np

FPS, SR = 30, 48000
# (сцена, [(субтитр, что читать голосу или None)])
SENTS = [
    (1, [('Договорённости теряются', None), ('в созвонах, документах и переписке.', None)]),
    (1, [('Primary Analytics превращает их', 'Праймари Аналитикс превращает их'), ('в память проекта.', None)]),
    (2, [('Загрузите запись созвона, PDF,', 'Загрузите запись созвона, пи-ди-эф,'), ('Word, скриншот или заметку.', 'Ворд, скриншот или заметку.')]),
    (2, [('Оригинал остаётся нетронутым.', None)]),
    (3, [('Речь распознают два независимых', None), ('распознавателя, Parakeet и GigaAM.', 'распознавателя, Паракит и ГигаЭйЭм.')]),
    (3, [('Sortformer и база голосов', 'Сортформер и база голосов'), ('определяют, кто говорит,', None), ('а нейросеть вычитывает спорные места.', None)]),
    (4, [('Встречу можно провести прямо в портале:', None)]),
    (4, [('у каждого участника своя дорожка записи,', None)]),
    (4, [('а реплики приписываются говорящему', None), ('без догадок.', None)]),
    (5, [('В основе проекта — векторная база.', None)]),
    (5, [('Модель bge-m3 превращает фрагменты', 'Модель би-джи-и эм три превращает фрагменты'), ('текста и сущности в векторы.', None)]),
    (5, [('Вопрос находит ближайшее по смыслу,', None), ('с учётом веса источника,', None)]),
    (5, [('а по близости строится граф', None), ('и выявляются противоречия.', None)]),
    (6, [('Менеджер получает протокол —', None), ('кто, что и к какому сроку,', None)]),
    (6, [('актуальное ТЗ', 'актуальное тэ зэ'), ('и чат, который отвечает со ссылкой на источник.', None)]),
    (7, [('Внешние ИИ-агенты подключаются', 'Внешние и-и агенты подключаются'), ('через MCP по ключам,', 'через эм-си-пи по ключам,')]),
    (7, [('а шлюз пропускает', None), ('только разрешённое.', None)]),
    (7, [('Роли и изоляция проектов встроены.', None)]),
    (8, [('Всё работает локально,', None), ('в вашей сети,', None), ('для команды до двадцати человек.', None)]),
]
LEAD, GAP, CHUNK_GAP, SCENE_GAP, TAIL = 6, 3, 2, 8, 18
os.makedirs('audio', exist_ok=True)


def load(path):
    with wave.open(path) as w:
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        x = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(1)
    if sr != SR:
        raw = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'f32le', '-ar', str(sr), '-ac', '1', '-i', '-', '-f', 'f32le', '-ar', str(SR), '-ac', '1', '-'],
                             input=x.tobytes(), capture_output=True, check=True).stdout
        x = np.frombuffer(raw, dtype=np.float32).copy()
    idx = np.where(np.abs(x) > 0.004)[0]
    a = max(0, idx[0] - int(.02 * SR))
    b = min(len(x), idx[-1] + int(.08 * SR))
    return x[a:b]


def main():
    flat = [(sc, si, sub, tts or sub) for si, (sc, chs) in enumerate(SENTS) for sub, tts in chs]
    json.dump([f[3] for f in flat], open('audio/texts.json', 'w', encoding='utf-8'), ensure_ascii=False)
    subprocess.run(['powershell', '-NoProfile', '-File', 'synth.ps1', 'audio/texts.json', os.path.abspath('audio')], check=True)
    t = LEAD / FPS
    parts, subs, starts = [], [], {}
    prev_sc = prev_si = None
    for i, (sc, si, sub, _) in enumerate(flat):
        x = load(f'audio/c{i:02d}.wav')
        if prev_si is not None:
            t += (SCENE_GAP if sc != prev_sc else GAP if si != prev_si else CHUNK_GAP) / FPS
        prev_sc, prev_si = sc, si
        dur = len(x) / SR
        f0 = int(round(t * FPS)) + 1
        starts.setdefault(sc, f0)
        subs.append({'from': f0, 'to': int(round((t + dur) * FPS)), 'text': sub})
        parts.append((t, x))
        t += dur
    total = int(np.ceil((t + TAIL / FPS) * FPS))
    y = np.zeros(int(total / FPS * SR) + SR, dtype=np.float32)
    for st, x in parts:
        a = int(st * SR)
        y[a:a + len(x)] += x
    y = y[:int(total / FPS * SR)]
    y = y / (float(np.max(np.abs(y))) or 1.0) * 0.89
    os.makedirs('public/assets', exist_ok=True)
    pcm = (np.clip(np.stack([y, y], 1), -1, 1) * 32767).astype(np.int16)
    with wave.open('public/assets/audio.wav', 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    for i in range(len(subs) - 1):
        if subs[i]['to'] >= subs[i + 1]['from']:
            subs[i]['to'] = subs[i + 1]['from'] - 1
    with open('src/timeline.ts', 'w', encoding='utf-8', newline='\n') as f:
        f.write('// auto: tts.py\n')
        f.write('export const TOTAL_FRAMES = %d;\n' % total)
        f.write('export const SCENE_FROM: Record<number, number> = %s;\n' % json.dumps(starts))
        f.write('export const SUBS: Array<{from: number; to: number; text: string}> = %s;\n' % json.dumps(subs, ensure_ascii=False))
    print('frames', total, '%.1fs' % (total / FPS), 'scene starts', starts)
    for s in subs:
        print(s)


main()
