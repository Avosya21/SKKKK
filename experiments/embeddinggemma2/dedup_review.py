"""Проверка дублей кадров: какие кадры можно не описывать, а взять описание у похожего.

Для каждого кадра считается «отпечаток» картинки (dHash: где соседние пиксели светлее/темнее)
в двух разрешениях — 16×16 (256 бит) и 32×32 (1024 бит, чувствительнее к мелким изменениям).
Страница out/dedup/review.html сама группирует кадры при выбранном пороге: кадр, который отличается
от уже оставленного меньше чем на порог, считается повтором. Владелец смотрит пары «оставили / выбросили»
и отмечает ошибки. GPU не нужен, портал не трогается.

    .venv\\Scripts\\python.exe dedup_review.py          # миниатюры + страница (≈1 мин)
    затем открыть out\\dedup\\review.html в браузере
"""
from __future__ import annotations

import json
from collections import defaultdict

import numpy as np
from PIL import Image

from common import OUT, read_jsonl

DST = OUT / "dedup"
THUMBS = DST / "thumbs"
BIG = DST / "big"  # копии для крупного сравнения: страница работает и с диска, и через локальный сервер
SECONDS_PER_FRAME = 40  # замер по логам воркера 13–14.10.2026


def dhash_hex(gray: Image.Image, n: int) -> str:
    g = np.asarray(gray.resize((n + 1, n), Image.LANCZOS), dtype=np.int16)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    return np.packbits(bits).tobytes().hex()


def main() -> None:
    THUMBS.mkdir(parents=True, exist_ok=True)
    BIG.mkdir(parents=True, exist_ok=True)
    frames = read_jsonl(OUT / "frames.jsonl")
    runs: dict[str, list] = defaultdict(list)
    for idx, f in enumerate(frames):
        run = f["frame_key"].rsplit(":", 1)[0]
        with Image.open(f["jpg"]) as im:
            rgb = im.convert("RGB")
            thumb = THUMBS / f"{idx}.jpg"
            if not thumb.exists():
                t = rgb.copy()
                t.thumbnail((480, 270))
                t.save(thumb, quality=78)
            big = BIG / f"{idx}.jpg"
            if not big.exists():
                b = rgb.copy()
                b.thumbnail((1600, 900))
                b.save(big, quality=82)
            gray = rgb.convert("L")
            h16, h32 = dhash_hex(gray, 16), dhash_hex(gray, 32)
        runs[run].append({"i": idx, "t": f.get("t_rel") or 0, "h16": h16, "h32": h32,
                          "desc": (f["text"] or "")[:300]})
    data = []
    for run, items in runs.items():
        items.sort(key=lambda x: x["t"])
        art = run.split(":")[0]
        data.append({"run": run, "project": next(f["project_id"] for f in frames if f["frame_key"].startswith(run + ":")),
                     "label": art, "frames": items})
    data.sort(key=lambda r: (r["project"], r["label"]))
    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False)).replace("__SPF__", str(SECONDS_PER_FRAME))
    (DST / "review.html").write_text(html, encoding="utf-8")
    print(f"[dedup] {DST / 'review.html'}: прогонов {len(data)}, кадров {len(frames)}")


TEMPLATE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Дубли кадров</title>
<style>
:root{--bg:#fafafa;--fg:#1b1b1b;--muted:#666;--card:#fff;--line:#ddd;--keep:#1a7f37;--drop:#b35900;--bad:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--muted:#9a9a9a;--card:#222;--line:#3a3a3a;--keep:#4ac26b;--drop:#e39b4a;--bad:#ef5350}}
body{background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif;margin:0;padding:16px}
header{position:sticky;top:0;background:var(--bg);padding:8px 0 12px;border-bottom:1px solid var(--line);z-index:5}
.ctl{display:flex;flex-wrap:wrap;gap:16px;align-items:center}
.stats{margin-top:8px;font-size:15px}.stats b{font-size:17px}
.run{margin:18px 0;padding:12px;background:var(--card);border:1px solid var(--line);border-radius:8px}
.run h3{margin:0 0 8px;font-size:15px}.muted{color:var(--muted)}
.group{display:flex;gap:6px;overflow-x:auto;padding:6px 0;border-top:1px dashed var(--line)}
.fr{flex:0 0 auto;width:200px;cursor:pointer;position:relative}
.fr img{width:200px;border-radius:4px;border:3px solid transparent;display:block}
.fr.keep img{border-color:var(--keep)}.fr.drop img{border-color:var(--drop);opacity:.85}
.fr.bad img{border-color:var(--bad);opacity:1}
.fr small{display:block;color:var(--muted);font-size:11px}
.fr.bad small{color:var(--bad);font-weight:600}
#modal{position:fixed;inset:0;background:rgba(0,0,0,.96);display:none;z-index:10;padding:12px;overflow:auto}
#modal .pair{display:flex;gap:10px;flex-wrap:wrap}#modal .pair div{flex:1 1 45%;min-width:280px;color:#eee}
#modal img{width:100%;border-radius:4px}#modal .btns{margin:10px 0;display:flex;gap:10px;flex-wrap:wrap}
button{font:inherit;padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.bad{background:var(--bad);color:#fff;border:0}
textarea{width:100%;height:70px;margin-top:8px;background:var(--card);color:var(--fg);border:1px solid var(--line)}
</style></head><body>
<header>
 <div class="ctl">
  <label>Отпечаток <select id="hash"><option value="h16">16×16 (грубее)</option><option value="h32">32×32 (чувствительнее)</option></select></label>
  <label>Порог: <input type="range" id="thr" min="0" max="20" step="1" value="3"> <b id="thrv"></b>% отличий</label>
  <label>Проект <select id="proj"><option value="">все</option></select></label>
  <label><input type="checkbox" id="onlydup" checked> только группы с повторами</label>
 </div>
 <div class="stats" id="stats"></div>
 <div class="muted">Зелёная рамка — кадр описываем, оранжевая — повтор, берёт описание у зелёного слева. Нажмите на оранжевый кадр, чтобы сравнить пару крупно и отметить ошибку.</div>
</header>
<main id="runs"></main>
<div id="modal"></div>
<section style="margin-top:24px"><b>Отмеченные ошибки</b> <span class="muted">(скопируйте и пришлите в чат)</span>
<textarea id="out" readonly></textarea></section>
<script>
const DATA=__DATA__, SPF=__SPF__;
const POP=new Uint8Array(256).map((_,i)=>{let c=0,x=i;while(x){c+=x&1;x>>=1}return c});
function dist(a,b){let d=0;for(let i=0;i<a.length;i+=2){d+=POP[parseInt(a.substr(i,2),16)^parseInt(b.substr(i,2),16)]}return d}
let bad={};try{bad=JSON.parse(localStorage.getItem('dedup_bad')||'{}')}catch(e){}
const $=id=>document.getElementById(id);
[...new Set(DATA.map(r=>r.project))].forEach(p=>{const o=document.createElement('option');o.value=p;o.textContent=p;$('proj').append(o)});
function group(run,key,thr){const bits=run.frames[0][key].length*4,lim=Math.round(bits*thr/100);const reps=[],groups=[];
 for(const f of run.frames){let best=-1,bd=1e9;reps.forEach((r,j)=>{const d=dist(f[key],r[key]);if(d<bd){bd=d;best=j}});
  if(best>=0&&bd<=lim){groups[best].push({f,d:bd,bits})}else{reps.push(f);groups.push([{f,d:0,bits,rep:true}])}}
 return groups}
function render(){const key=$('hash').value,thr=+$('thr').value,proj=$('proj').value,only=$('onlydup').checked;$('thrv').textContent=thr;
 let tot=0,kept=0;const root=$('runs');root.innerHTML='';
 for(const run of DATA){if(proj&&String(run.project)!==proj)continue;const gs=group(run,key,thr);tot+=run.frames.length;kept+=gs.length;
  const box=document.createElement('div');box.className='run';
  box.innerHTML=`<h3>Проект ${run.project} · ${run.label} <span class="muted">кадров ${run.frames.length} → описывать ${gs.length}</span></h3>`;
  for(const g of gs){if(only&&g.length<2)continue;const row=document.createElement('div');row.className='group';
   for(const it of g){const f=it.f,isBad=bad[f.i];const d=document.createElement('div');d.className='fr '+(it.rep?'keep':'drop')+(isBad?' bad':'');
    d.innerHTML=`<img loading="lazy" src="thumbs/${f.i}.jpg"><small>#${f.i} · ${Math.round(f.t)} с${it.rep?'':' · отличие '+(100*it.d/it.bits).toFixed(1)+'%'}${isBad?' · ОШИБКА':''}</small>`;
    if(!it.rep)d.onclick=()=>openPair(g[0].f,f,it);row.append(d)}
   box.append(row)}
  root.append(box)}
 const saved=tot-kept;$('stats').innerHTML=`Кадров <b>${tot}</b> → описывать <b>${kept}</b> (${tot?Math.round(100*kept/tot):0}%). Экономия ≈ <b>${Math.round(saved*SPF/60)}</b> мин работы Qwen; отмечено ошибок: <b>${Object.keys(bad).length}</b>`;
 $('out').value=Object.entries(bad).map(([i,v])=>`#${i} ≠ #${v.rep} (${v.hash}, отличие ${v.pct}%)`).join('\n')}
function openPair(rep,f,it){const m=$('modal');const pct=(100*it.d/it.bits).toFixed(1);
 m.innerHTML=`<div class="btns"><button class="bad" id="mb">Разные — нельзя выбрасывать</button><button id="mo">Одинаковые по смыслу</button><button id="mc">Закрыть</button>
 <span style="color:#eee">отличие ${pct}% · Esc — закрыть</span></div><div class="pair"><div><b>Оставлен #${rep.i}</b> (${Math.round(rep.t)} с)<a href="big/${rep.i}.jpg" target="_blank"><img src="big/${rep.i}.jpg"></a><p>${rep.desc.replace(/</g,'&lt;')}</p></div>
 <div><b>Повтор #${f.i}</b> (${Math.round(f.t)} с)<a href="big/${f.i}.jpg" target="_blank"><img src="big/${f.i}.jpg"></a><p>${f.desc.replace(/</g,'&lt;')}</p></div></div>`;
 m.style.display='block';const close=()=>{m.style.display='none';render()};
 $('mb').onclick=()=>{bad[f.i]={rep:rep.i,hash:$('hash').value,pct};save();close()};
 $('mo').onclick=()=>{delete bad[f.i];save();close()};$('mc').onclick=close}
function save(){try{localStorage.setItem('dedup_bad',JSON.stringify(bad))}catch(e){}}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){$('modal').style.display='none'}});
['hash','thr','proj','onlydup'].forEach(id=>$(id).oninput=render);render();
</script></body></html>
"""

if __name__ == "__main__":
    main()
