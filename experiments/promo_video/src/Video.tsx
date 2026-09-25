import React from 'react';
import {AbsoluteFill, Audio, Easing, continueRender, delayRender, interpolate, staticFile, useCurrentFrame} from 'remotion';
import {SCENE_FROM, SUBS, TOTAL_FRAMES} from './timeline';

// ---------- палитра и утилиты ----------
const PURPLE = '#6630F8';
const PURPLE_L = '#A175F1';
const ORANGE = '#F05F41';
const GREEN = '#8FF740';
const CYAN = '#58FFEE';
const GREY = '#A0A0A1';
const GREY_D = '#4A4A4A';
const FONT = `'Noto Sans SC', sans-serif`;
const FONT_ORB = `'Orbitron', sans-serif`;

const out = Easing.bezier(0.22, 1, 0.36, 1);
const inOut = Easing.bezier(0.65, 0, 0.35, 1);
const k = (f: number, a: number, len = 12, ease = out) => interpolate(f, [a, a + len], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: ease});
const B = SUBS.map((s) => s.from); // моменты появления субтитр-блоков = ритм озвучки

const Fonts: React.FC = () => {
  const [h] = React.useState(() => delayRender('fonts'));
  React.useEffect(() => {
    Promise.all([
      new FontFace('Noto Sans SC', `url(${staticFile('fonts/NotoSansSC.ttf')})`, {weight: '100 900'} as FontFaceDescriptors).load(),
      new FontFace('Orbitron', `url(${staticFile('fonts/Orbitron[wght].ttf')})`, {weight: '400 900'} as FontFaceDescriptors).load(),
    ])
      .then((fs) => {
        fs.forEach((f) => (document.fonts as unknown as {add: (f: FontFace) => void}).add(f));
        continueRender(h);
      })
      .catch(() => continueRender(h));
  }, [h]);
  return null;
};

// ---------- фон: точечная волна ----------
const Dots: React.FC<{f: number}> = ({f}) => {
  const cols = 33, rows = 19;
  const els: React.ReactNode[] = [];
  for (let i = 0; i < cols; i++) {
    for (let j = 0; j < rows; j++) {
      const x = 20 + i * 39.5, y = 20 + j * 39;
      const w = Math.sin(i * 0.35 + f * 0.05) * Math.cos(j * 0.3 - f * 0.035);
      els.push(<circle key={`${i}-${j}`} cx={x} cy={y} r={1.1 + (w + 1) * 0.9} fill={PURPLE} opacity={0.10 + (w + 1) * 0.10} />);
    }
  }
  return <svg width={1280} height={720} style={{position: 'absolute', left: 0, top: 0}}>{els}</svg>;
};

// ---------- примитивы ----------
const Box: React.FC<{x: number; y: number; w: number; h: number; r?: number; stroke?: string; fill?: string; glow?: string; dashed?: boolean; op?: number; children?: React.ReactNode}> = ({x, y, w, h, r = 14, stroke = '#fff', fill = '#000', glow, dashed, op = 1, children}) => (
  <div style={{position: 'absolute', left: x, top: y, width: w, height: h, borderRadius: r, background: fill, border: `2.5px ${dashed ? 'dashed' : 'solid'} ${stroke}`, boxShadow: glow ? `0 0 26px 4px ${glow}` : undefined, opacity: op, boxSizing: 'border-box'}}>{children}</div>
);
const T: React.FC<{cx: number; cy: number; size: number; color?: string; weight?: number; op?: number; family?: string; children: React.ReactNode; glow?: string}> = ({cx, cy, size, color = '#fff', weight = 800, op = 1, family = FONT, children, glow}) => (
  <div style={{position: 'absolute', left: cx, top: cy, transform: 'translate(-50%,-50%)', fontFamily: family, fontSize: size, fontWeight: weight, color, opacity: op, whiteSpace: 'nowrap', textShadow: glow, lineHeight: 1}}>{children}</div>
);
const Bar: React.FC<{x: number; y: number; w: number; h?: number; color?: string; p?: number; op?: number}> = ({x, y, w, h = 8, color = GREY, p = 1, op = 1}) => (
  <div style={{position: 'absolute', left: x, top: y, width: w * p, height: h, borderRadius: h / 2, background: color, opacity: op}} />
);
const Arrow: React.FC<{x0: number; y0: number; x1: number; y1: number; p: number; color?: string}> = ({x0, y0, x1, y1, p, color = '#fff'}) => {
  if (p <= 0) return null;
  const ex = x0 + (x1 - x0) * p, ey = y0 + (y1 - y0) * p;
  const a = Math.atan2(y1 - y0, x1 - x0);
  const hx = (d: number) => ex - 14 * Math.cos(a + d), hy = (d: number) => ey - 14 * Math.sin(a + d);
  return (
    <svg width={1280} height={720} style={{position: 'absolute', left: 0, top: 0}}>
      <line x1={x0} y1={y0} x2={ex} y2={ey} stroke={color} strokeWidth={3} strokeLinecap="round" />
      {p > 0.9 ? <polyline points={`${hx(0.5)},${hy(0.5)} ${ex},${ey} ${hx(-0.5)},${hy(-0.5)}`} fill="none" stroke={color} strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" /> : null}
    </svg>
  );
};
const Wave: React.FC<{x: number; y: number; w: number; h: number; f: number; n?: number; amp?: number; color?: string; seed?: number}> = ({x, y, w, h, f, n = 28, amp = 1, color = '#fff', seed = 0}) => amp <= 0.04 ? null : (
  <svg width={1280} height={720} style={{position: 'absolute', left: 0, top: 0}}>
    {Array.from({length: n}).map((_, i) => {
      const v = (0.35 + 0.65 * Math.abs(Math.sin(i * 0.9 + f * 0.22 + seed) * Math.cos(i * 0.37 - f * 0.13))) * amp;
      const bh = Math.max(3, h * v);
      const bw = w / n;
      return <rect key={i} x={x + i * bw + bw * 0.18} y={y - bh / 2} width={bw * 0.64} height={bh} rx={bw * 0.32} fill={color} />;
    })}
  </svg>
);
const Pill: React.FC<{cx: number; cy: number; w: number; h?: number; text: string; color?: string; size?: number; op?: number; scale?: number}> = ({cx, cy, w, h = 40, text, color = PURPLE, size = 22, op = 1, scale = 1}) => (
  <div style={{position: 'absolute', left: cx - w / 2, top: cy - h / 2, width: w, height: h, borderRadius: h / 2, background: color, opacity: op, transform: `scale(${scale})`, display: 'flex', alignItems: 'center', justifyContent: 'center', whiteSpace: 'nowrap', fontFamily: FONT, fontWeight: 800, fontSize: size, color: color === GREEN || color === CYAN ? '#000' : '#fff', boxShadow: `0 0 18px ${color}88`}}>{text}</div>
);

// ---------- ритм: b(сцена, блок) = кадр начала субтитр-блока ----------
const SC_N = [4, 3, 5, 5, 7, 4, 5, 3];
const OFF = SC_N.map((_, i) => SC_N.slice(0, i).reduce((a, c) => a + c, 0));
const b = (s: number, i: number) => B[OFF[s] + i];
const HDR = (text: string, w: number, f: number, at: number) => <Pill cx={640} cy={62} w={w} text={text} op={k(f, at, 10)} />;
const Check: React.FC<{cx: number; cy: number; p: number; color?: string}> = ({cx, cy, p, color = GREEN}) =>
  p <= 0 ? null : (
    <svg width={1280} height={720} style={{position: 'absolute', left: 0, top: 0, opacity: Math.min(1, p * 2)}}>
      <polyline points={`${cx - 9},${cy} ${cx - 2},${cy + 8} ${cx + 11},${cy - 9}`} fill="none" stroke={color} strokeWidth={4} strokeLinecap="round" strokeLinejoin="round" strokeDasharray={40} strokeDashoffset={40 * (1 - p)} />
    </svg>
  );
const Avatar: React.FC<{cx: number; cy: number; r?: number; o?: number; color?: string}> = ({cx, cy, r = 30, o = 1, color = '#fff'}) => (
  <svg width={1280} height={720} style={{position: 'absolute', left: 0, top: 0, opacity: o}}>
    <circle cx={cx} cy={cy} r={r} fill="#000" stroke={color} strokeWidth={3} />
    <circle cx={cx} cy={cy - r * 0.15} r={r * 0.33} fill={color} />
    <path d={`M ${cx - r * 0.6} ${cy + r * 0.62} Q ${cx} ${cy + r * 0.05} ${cx + r * 0.6} ${cy + r * 0.62}`} fill="none" stroke={color} strokeWidth={3} />
  </svg>
);

// ---------- 1. проблема ----------
const Scene1: React.FC<{f: number}> = ({f}) => {
  const n = f - b(0, 0);
  const bub = [b(0, 1) - 12, b(0, 1) + 6, b(0, 1) + 24];
  const qs = ['Кто это обещал?', 'Какой срок?', 'Что решили?'];
  const gone = 1 - k(f, b(0, 2) - 14, 12);
  const decay = 1 - 0.85 * k(f, b(0, 1) + 20, 40, inOut);
  const wm = k(f, b(0, 2) - 4, 16);
  return (
    <>
      <Pill cx={640} cy={62} w={230} text="СОЗВОН ЗАВЕРШЁН" op={k(n, 0, 10) * gone} />
      <div style={{opacity: gone}}>
        {[360, 640, 920].map((cx, i) => <Avatar key={i} cx={cx} cy={200 - (1 - k(n, 6 + i * 5, 12)) * 20} o={k(n, 6 + i * 5, 12)} />)}
        {qs.map((q, i) => {
          const o = k(f, bub[i], 10);
          return (
            <React.Fragment key={i}>
              <Box x={[360, 640, 920][i] - 120} y={278 + (1 - o) * 14} w={240} h={58} r={16} stroke={ORANGE} dashed op={o} glow={o > 0.5 ? 'rgba(240,95,65,.35)' : undefined} />
              <T cx={[360, 640, 920][i]} cy={308 + (1 - o) * 14} size={24} color={ORANGE} op={o}>{q}</T>
            </React.Fragment>
          );
        })}
        <Wave x={300} y={480} w={680} h={150} f={f} n={44} amp={decay} color={decay < 0.4 ? GREY_D : '#fff'} />
      </div>
      <T cx={640} cy={290} size={78} family={FONT_ORB} weight={800} op={wm} glow={`0 0 ${30 * wm}px rgba(102,48,248,.9), 0 0 ${80 * wm}px rgba(102,48,248,.5)`}>Primary Analytics</T>
      <Pill cx={640} cy={390} w={300} h={46} size={24} text="память проекта" op={k(f, b(0, 3), 12)} scale={0.85 + 0.15 * k(f, b(0, 3), 12)} />
    </>
  );
};

// ---------- 2. загрузка ----------
const Scene2: React.FC<{f: number}> = ({f}) => {
    const src = [{x: 180, t: 'Созвон', c: '#fff'}, {x: 410, t: 'PDF', c: ORANGE}, {x: 640, t: 'Word', c: PURPLE_L}, {x: 870, t: 'Скрин', c: CYAN}, {x: 1100, t: 'Заметка', c: GREEN}];
  const proj = k(f, b(1, 0) + 4, 14);
  const lock = k(f, b(1, 2) - 4, 12);
  return (
    <>
      {HDR('ЗАГРУЗКА', 200, f, b(1, 0) - 8)}
      {src.map((s, i) => {
        const a = k(f, b(1, 0) + 6 + i * 7, 14);
        const arr = k(f, b(1, 1) + i * 5 - 6, 12);
        return (
          <React.Fragment key={i}>
            <Box x={s.x - 75} y={150 - (1 - a) * 40} w={150} h={120} r={12} stroke={s.c} op={a} />
            {i === 0 ? <div style={{opacity: a}}><Wave x={s.x - 55} y={200 - (1 - a) * 40} w={110} h={50} f={f} n={10} color="#fff" /></div> : <><Bar x={s.x - 45} y={180 - (1 - a) * 40} w={90} color={s.c} op={a} /><Bar x={s.x - 45} y={198 - (1 - a) * 40} w={64} color={GREY_D} op={a} /></>}
            <T cx={s.x} cy={244 - (1 - a) * 40} size={22} color={s.c} op={a}>{s.t}</T>
            <Arrow x0={s.x} y0={278} x1={640 + (s.x - 640) * 0.25} y1={392} p={arr} color={s.c} />
          </React.Fragment>
        );
      })}
      <Box x={430} y={400} w={420} h={150} r={18} stroke={PURPLE_L} op={proj} glow={proj > 0.6 ? 'rgba(102,48,248,.4)' : undefined} />
      <T cx={640} cy={432} size={36} family={FONT} weight={900} op={proj}>Проект</T>
      <div style={{opacity: lock}}>
        <svg width={1280} height={720} style={{position: 'absolute', left: 0, top: 0}}>
          <rect x={612} y={486} width={56} height={40} rx={8} fill="#000" stroke={GREEN} strokeWidth={3.5} />
          <path d="M 624 486 v -12 a 16 16 0 0 1 32 0 v 12" fill="none" stroke={GREEN} strokeWidth={3.5} />
          <circle cx={640} cy={506} r={5} fill={GREEN} />
        </svg>
      </div>
      <Pill cx={640} cy={588} w={330} h={40} size={20} text="оригинал не меняется" color={GREEN} op={lock} scale={0.9 + 0.1 * lock} />
    </>
  );
};

// ---------- 3. расшифровка ----------
const Scene3: React.FC<{f: number}> = ({f}) => {
  const s = 2;
  const st = SCENE_FROM[3];
  const rec = k(f, st, 12);
  const asr = [k(f, b(s, 1) - 10, 12), k(f, b(s, 1) - 2, 12)];
  const sf = k(f, b(s, 2) - 2, 12);
  const vb = k(f, b(s, 2) + 12, 12);
  const tr = k(f, b(s, 3) - 6, 12);
  const llm = k(f, b(s, 4) - 4, 14);
  const fix = k(f, b(s, 4) + 14, 16);
  const spk = [PURPLE_L, ORANGE, CYAN];
  return (
    <>
      {HDR('ДВОЙНОЕ РАСПОЗНАВАНИЕ', 340, f, st)}
      <Box x={40} y={270} w={210} h={150} stroke="#fff" op={rec} />
      <Wave x={58} y={345} w={174} h={70} f={f} n={16} color="#fff" />
      <T cx={145} cy={445} size={22} color={GREY} op={rec}>запись</T>
      <Arrow x0={252} y0={330} x1={326} y1={270} p={k(f, b(s, 1) - 14, 14)} />
      <Arrow x0={252} y0={360} x1={326} y1={420} p={k(f, b(s, 1) - 8, 14)} />
      {[{y: 205, name: 'Parakeet', c: PURPLE_L, sd: 0}, {y: 355, name: 'GigaAM', c: CYAN, sd: 2}].map((bx, i) => (
        <React.Fragment key={i}>
          <Box x={330} y={bx.y} w={240} h={130} stroke={bx.c} op={asr[i]} glow={asr[i] > 0.6 ? `${bx.c}55` : undefined} />
          <T cx={450} cy={bx.y + 32} size={30} family={FONT_ORB} weight={700} color={bx.c} op={asr[i]}>{bx.name}</T>
          <div style={{opacity: asr[i]}}><Wave x={350} y={bx.y + 86} w={200} h={50} f={f} n={18} color={bx.c} seed={bx.sd} /></div>
        </React.Fragment>
      ))}
      <Box x={330} y={510} w={240} h={66} r={14} stroke={ORANGE} op={sf} glow={sf > 0.6 ? 'rgba(240,95,65,.4)' : undefined} />
      <T cx={450} cy={543} size={24} family={FONT_ORB} weight={700} color={ORANGE} op={sf}>Sortformer</T>
      <Box x={600} y={510} w={230} h={66} r={14} stroke={GREEN} op={vb} />
      <T cx={715} cy={543} size={24} color={GREEN} op={vb}>база голосов</T>
      <Arrow x0={572} y0={270} x1={648} y1={318} p={k(f, b(s, 4) - 8, 12)} color={PURPLE_L} />
      <Arrow x0={572} y0={420} x1={648} y1={372} p={k(f, b(s, 4) - 8, 12)} color={CYAN} />
      <div style={{position: 'absolute', left: 660, top: 283, width: 124, height: 124, borderRadius: 62, border: '3px solid #fff', background: '#000', boxShadow: `0 0 ${20 + 26 * llm}px ${6 * llm}px rgba(102,48,248,${0.55 * llm})`, opacity: llm, transform: `scale(${0.7 + 0.3 * llm})`}} />
      <T cx={722} cy={345} size={34} family={FONT_ORB} weight={800} op={llm}>LLM</T>
      <Arrow x0={790} y0={345} x1={848} y1={345} p={k(f, b(s, 4) + 4, 12)} />
      <Box x={856} y={130} w={384} h={430} stroke="#fff" op={tr} />
      <T cx={1048} cy={162} size={22} color={GREY} op={tr}>стенограмма</T>
      {[0, 1, 2, 3].map((i) => {
        const ci = [0, 1, 0, 2][i];
        const y = 200 + i * 86;
        const a = k(f, b(s, 3) - 2 + i * 8, 10);
        const lw = [250, 200, 270, 180][i];
        return (
          <React.Fragment key={i}>
            <Pill cx={912} cy={y + 14} w={92} h={28} size={17} text={`Спикер ${ci + 1}`} color={spk[ci]} op={a} />
            <Bar x={870} y={y + 40} w={lw + 40} p={a} color={fix > 0.5 && i % 2 === 1 ? '#fff' : GREY} h={8} />
            <Bar x={870} y={y + 56} w={lw - 30} p={a} color={GREY_D} h={8} />
          </React.Fragment>
        );
      })}
      <Check cx={1215} cy={190} p={fix} />
    </>
  );
};

// ---------- 4. комната созвона ----------
const Scene4: React.FC<{f: number}> = ({f}) => {
  const s = 3;
  const room = k(f, SCENE_FROM[4] - 2, 14);
  const scr = k(f, b(s, 2) - 4, 14);
  const rows = [{y: 185, n: 'Участник 1', c: PURPLE_L, sd: 0}, {y: 295, n: 'Участник 2', c: ORANGE, sd: 3}, {y: 405, n: 'Гость', c: CYAN, sd: 6}];
  const rep = k(f, b(s, 3) - 4, 12);
  const unk = k(f, b(s, 4) + 4, 12);
  const FR = [0, 1, 2, 3, 4, 5];
  const DROP = new Set([1, 4]);
  const speakerCol = [PURPLE_L, ORANGE, CYAN];
  return (
    <>
      {HDR('КОМНАТА СОЗВОНА', 300, f, SCENE_FROM[4] - 4)}
      <Box x={70} y={115} w={690} h={430} r={18} stroke="#fff" op={room} />
      <div style={{position: 'absolute', left: 96, top: 136, width: 12, height: 12, borderRadius: 6, background: ORANGE, opacity: room, boxShadow: '0 0 10px 3px rgba(240,95,65,.7)'}} />
      <T cx={250} cy={142} size={20} color={GREY} op={room}>идёт запись дорожек</T>
      {rows.map((r, i) => {
        const a = k(f, b(s, 1) - 6 + i * 10, 14);
        return (
          <React.Fragment key={i}>
            <Box x={92} y={r.y} w={646} h={92} r={12} stroke={r.c} op={a} />
            <Avatar cx={140} cy={r.y + 46} r={26} o={a} color={r.c} />
            <T cx={272} cy={r.y + 46} size={22} color={r.c} op={a}>{r.n}</T>
            <Wave x={380} y={r.y + 46} w={340} h={52} f={f} n={30} amp={a} color={r.c} seed={r.sd} />
          </React.Fragment>
        );
      })}
      <Box x={800} y={115} w={400} h={170} r={16} stroke={CYAN} op={scr} glow={scr > 0.6 ? 'rgba(88,255,238,.25)' : undefined} />
      <T cx={1000} cy={148} size={20} color={CYAN} op={scr}>кадры экрана</T>
      {FR.map((i) => {
        const bx = 826 + i * 56;
        const appear = k(f, b(s, 2) + i * 3, 8);
        const drop = DROP.has(i) ? k(f, b(s, 2) + 16 + i * 2, 10) : 0;
        if (appear <= 0) return null;
        return (
          <React.Fragment key={i}>
            <div style={{position: 'absolute', left: bx, top: 178, width: 48, height: 34, borderRadius: 4, border: `2px solid ${DROP.has(i) ? GREY_D : CYAN}`, opacity: appear * (1 - drop), background: '#000'}} />
            {!DROP.has(i) ? <div style={{position: 'absolute', left: bx + 16, top: 220, width: 16, height: 6, borderRadius: 3, background: speakerCol[i % 3], opacity: appear}} /> : null}
          </React.Fragment>
        );
      })}
      <T cx={1000} cy={252} size={15} color={GREY} op={scr}>смена сцены · к говорящему</T>
      <Box x={800} y={310} w={400} h={235} r={16} stroke="#fff" op={rep} />
      <T cx={1000} cy={340} size={20} color={GREY} op={rep}>реплики по дорожкам</T>
      {[0, 1, 2].map((i) => {
        const a = k(f, b(s, 3) + i * 8, 10);
        const c = [PURPLE_L, ORANGE, CYAN][i];
        return (
          <React.Fragment key={i}>
            <Pill cx={862} cy={385 + i * 46} w={92} h={28} size={16} text={['Уч. 1', 'Уч. 2', 'Гость'][i]} color={c} op={a} />
            <Bar x={922} y={381 + i * 46} w={250 - i * 40} p={a} color={GREY} h={9} />
          </React.Fragment>
        );
      })}
      <Pill cx={1000} cy={530} w={330} h={36} size={18} text="не уверены → UNKNOWN, не имя" color={ORANGE} op={unk} scale={0.9 + 0.1 * unk} />
    </>
  );
};

// ---------- 5. векторная база ----------
const VPTS: Array<{x: number; y: number; t: 'f' | 'e'; c: string}> = [
  {x: 735, y: 215, t: 'f', c: PURPLE_L}, {x: 775, y: 185, t: 'f', c: PURPLE_L}, {x: 805, y: 240, t: 'e', c: PURPLE_L}, {x: 760, y: 275, t: 'f', c: PURPLE_L}, {x: 715, y: 255, t: 'f', c: PURPLE_L}, {x: 820, y: 205, t: 'f', c: PURPLE_L},
  {x: 1060, y: 230, t: 'f', c: GREEN}, {x: 1100, y: 268, t: 'e', c: GREEN}, {x: 1145, y: 215, t: 'f', c: GREEN}, {x: 1085, y: 305, t: 'f', c: GREEN}, {x: 1155, y: 295, t: 'f', c: GREEN}, {x: 1040, y: 280, t: 'e', c: GREEN},
  {x: 860, y: 440, t: 'f', c: CYAN}, {x: 912, y: 480, t: 'e', c: CYAN}, {x: 960, y: 430, t: 'f', c: CYAN}, {x: 890, y: 515, t: 'f', c: CYAN}, {x: 990, y: 490, t: 'f', c: CYAN}, {x: 935, y: 395, t: 'e', c: CYAN},
  {x: 690, y: 420, t: 'f', c: GREY}, {x: 1185, y: 425, t: 'f', c: GREY}, {x: 1000, y: 165, t: 'f', c: GREY}, {x: 1030, y: 525, t: 'f', c: GREY},
];
const VQ = {x: 930, y: 330};
const VNEAR = VPTS.map((p, i) => ({i, d: Math.hypot(p.x - VQ.x, p.y - VQ.y)})).sort((a, c) => a.d - c.d).slice(0, 5).map((o) => o.i);
const VEDGES: Array<[number, number]> = [[0, 1], [1, 5], [0, 4], [4, 3], [3, 2], [6, 8], [8, 7], [7, 10], [10, 9], [9, 11], [12, 13], [13, 14], [14, 17], [15, 13]];
const Scene5: React.FC<{f: number}> = ({f}) => {
  const s = 4;
  const st = SCENE_FROM[5];
  const space = k(f, b(s, 0) - 2, 14);
  const cards = [{y: 150, t: 'фрагмент', c: GREY}, {y: 240, t: 'фрагмент', c: GREY}, {y: 330, t: 'сущность', c: GREEN}];
  const model = k(f, b(s, 1) + 8, 14);
  const vec = k(f, b(s, 2) - 6, 12);
  const qOp = k(f, b(s, 3) - 2, 12) * (1 - k(f, b(s, 5) - 4, 10));
  const ring = k(f, b(s, 3) + 4, 26, inOut);
  const wt = k(f, b(s, 4), 12) * (1 - k(f, b(s, 5) - 4, 10));
  const gr = b(s, 5);
  const cf = k(f, b(s, 6) - 2, 12);
  return (
    <>
      {HDR('ВЕКТОРНАЯ БАЗА', 280, f, st - 4)}
      {cards.map((c, i) => {
        const a = k(f, b(s, 1) - 2 + i * 6, 14);
        return (
          <React.Fragment key={i}>
            <Box x={50} y={c.y} w={250} h={70} r={12} stroke={c.c} op={a} />
            <T cx={125} cy={c.y + 24} size={20} color={c.c === GREY ? '#fff' : c.c} op={a}>{c.t}</T>
            <Bar x={70} y={c.y + 44} w={i === 2 ? 130 : 200} color={GREY_D} op={a} />
            <Arrow x0={302} y0={c.y + 35} x1={378} y1={275} p={k(f, b(s, 1) + 6 + i * 4, 12)} color={c.c === GREY ? '#fff' : c.c} />
          </React.Fragment>
        );
      })}
      <T cx={175} cy={125} size={19} color={GREY} op={k(f, b(s, 1), 12)}>фрагменты ≈ 512 токенов</T>
      <Box x={384} y={205} w={220} h={140} r={16} stroke={PURPLE_L} op={model} glow={model > 0.6 ? 'rgba(102,48,248,.4)' : undefined} />
      <T cx={494} cy={250} size={30} family={FONT_ORB} weight={800} color={PURPLE_L} op={model}>bge-m3</T>
      <T cx={494} cy={296} size={19} color={GREY} op={model}>Ollama · CPU</T>
      <T cx={494} cy={390} size={19} color={GREY} family={`'SF Mono', Consolas, monospace`} op={vec}>[ 0.12  −0.83  0.41 … ]</T>
      <Pill cx={494} cy={430} w={230} h={36} size={18} text="вектор · 1024 числа" color={PURPLE} op={vec} scale={0.9 + 0.1 * vec} />
      <Arrow x0={606} y0={275} x1={646} y1={275} p={k(f, b(s, 2) - 8, 10)} />
      <Box x={650} y={110} w={580} h={450} r={18} stroke="#fff" op={space} glow={space > 0.6 ? 'rgba(102,48,248,.3)' : undefined} />
      <T cx={940} cy={140} size={20} color={GREY} op={space}>sqlite-vec · поиск по смыслу</T>
      {VEDGES.map(([a, c2], i) => {
        const p = k(f, gr + i * 3, 12);
        if (p <= 0) return null;
        const A = VPTS[a], C = VPTS[c2];
        return <svg key={i} width={1280} height={720} style={{position: 'absolute', left: 0, top: 0}}><line x1={A.x} y1={A.y} x2={A.x + (C.x - A.x) * p} y2={A.y + (C.y - A.y) * p} stroke={A.c} strokeWidth={2.5} opacity={0.75} /></svg>;
      })}
      {cf > 0 ? <svg width={1280} height={720} style={{position: 'absolute', left: 0, top: 0, opacity: cf}}><line x1={VPTS[2].x} y1={VPTS[2].y} x2={VPTS[11].x} y2={VPTS[11].y} stroke={ORANGE} strokeWidth={3.5} strokeDasharray="9 7" /></svg> : null}
      {VPTS.map((pt, i) => {
        const a = k(f, b(s, 2) + i * 2, 12);
        const near = VNEAR.includes(i) && qOp > 0.3;
        const hot = cf > 0.5 && (i === 2 || i === 11);
        const col = hot ? ORANGE : near ? '#fff' : pt.c;
        const sz = pt.t === 'e' ? 22 : 14;
        return <div key={i} style={{position: 'absolute', left: pt.x - sz / 2, top: pt.y - sz / 2, width: sz, height: sz, borderRadius: pt.t === 'e' ? 6 : sz / 2, background: pt.t === 'e' ? '#000' : col, border: `3px solid ${col}`, opacity: a * (pt.c === GREY ? 0.6 : 1), transform: `scale(${(0.4 + 0.6 * a) * (near ? 1.25 : 1)})`, boxShadow: near ? '0 0 16px 3px rgba(255,255,255,.55)' : undefined}} />;
      })}
      {qOp > 0 ? (
        <svg width={1280} height={720} style={{position: 'absolute', left: 0, top: 0, opacity: qOp}}>
          <circle cx={VQ.x} cy={VQ.y} r={20 + ring * 150} fill="none" stroke={ORANGE} strokeWidth={2.5} opacity={0.5 * (1 - ring * 0.6)} />
          {VNEAR.map((i, j) => <line key={j} x1={VQ.x} y1={VQ.y} x2={VPTS[i].x} y2={VPTS[i].y} stroke={ORANGE} strokeWidth={2.5} strokeDasharray="6 6" opacity={k(f, b(s, 3) + 10 + j * 3, 8)} />)}
          <circle cx={VQ.x} cy={VQ.y} r={11} fill={ORANGE} />
          <circle cx={VQ.x} cy={VQ.y} r={20} fill="none" stroke={ORANGE} strokeWidth={3} />
        </svg>
      ) : null}
      <Pill cx={VQ.x} cy={VQ.y - 44} w={100} h={30} size={17} text="вопрос" color={ORANGE} op={qOp} />
      {[{cx: 1085, cy: 345, t: 'документ ×1.3', c: GREEN}, {cx: 770, cy: 478, t: 'созвон ×1.0', c: PURPLE}].map((w, j) => (
        <Pill key={j} cx={w.cx} cy={w.cy} w={140} h={28} size={16} text={w.t} color={w.c} op={wt} />
      ))}
      <Pill cx={935} cy={200} w={190} h={36} size={18} text="конфликт версий" color={ORANGE} op={cf} scale={0.9 + 0.1 * cf} />
      <T cx={940} cy={540} size={18} color={GREY} op={k(f, b(s, 2) + 20, 12)}>круги — фрагменты · квадраты — сущности</T>
    </>
  );
};

// ---------- 6. для менеджера ----------
const Scene6: React.FC<{f: number}> = ({f}) => {
  const s = 5;
  const proto = k(f, SCENE_FROM[6] - 2, 14);
  const tz = k(f, b(s, 2) - 2, 14);
  const chat = k(f, b(s, 3) - 2, 14);
  const cols = [{t: 'Кто', x: 110}, {t: 'Что', x: 290}, {t: 'Срок', x: 540}];
  return (
    <>
      {HDR('ДЛЯ МЕНЕДЖЕРА', 260, f, SCENE_FROM[6] - 4)}
      <Box x={50} y={115} w={580} h={300} r={16} stroke="#fff" op={proto} />
      <T cx={130} cy={150} size={24} color={PURPLE_L} op={proto}>Протокол</T>
      {cols.map((c, i) => <T key={i} cx={c.x} cy={192} size={19} color={GREY} op={proto}>{c.t}</T>)}
      <div style={{position: 'absolute', left: 70, top: 210, width: 540, height: 2, background: GREY_D, opacity: proto}} />
      {[0, 1, 2, 3].map((i) => {
        const a = k(f, b(s, 1) - 6 + i * 7, 12);
        return (
          <React.Fragment key={i}>
            <Pill cx={110} cy={244 + i * 46} w={80} h={26} size={15} text={['Анна', 'Игорь', 'Елена', 'Олег'][i]} color={[PURPLE_L, ORANGE, CYAN, GREEN][i]} op={a} />
            <Bar x={170} y={240 + i * 46} w={240} p={a} color={GREY} h={9} />
            <Pill cx={540} cy={244 + i * 46} w={100} h={26} size={15} text={['до пт', '12.10', 'до 30.10', 'завтра'][i]} color={PURPLE} op={a} />
          </React.Fragment>
        );
      })}
      <Box x={660} y={115} w={570} h={300} r={16} stroke={PURPLE_L} op={tz} glow={tz > 0.6 ? 'rgba(102,48,248,.35)' : undefined} />
      <T cx={725} cy={155} size={34} weight={900} color={PURPLE_L} op={tz}>ТЗ</T>
      <Pill cx={1150} cy={155} w={110} h={28} size={16} text="актуально" color={GREEN} op={tz} />
      {[0, 1, 2, 3, 4].map((i) => <Bar key={i} x={690} y={205 + i * 40} w={[500, 430, 510, 360, 460][i]} p={k(f, b(s, 2) + 4 + i * 4, 14)} color={i % 2 ? GREY : '#fff'} h={9} />)}
      <Box x={50} y={440} w={1180} h={150} r={18} stroke="#fff" op={chat} />
      <Avatar cx={100} cy={478} r={20} o={chat} />
      <Bar x={140} y={470} w={420} h={12} color="#fff" p={k(f, b(s, 3), 12)} />
      <Box x={90} y={505} w={1110} h={70} r={14} stroke={PURPLE_L} op={k(f, b(s, 3) + 10, 12)} />
      <Bar x={112} y={527} w={520} h={10} color={GREY} p={k(f, b(s, 3) + 14, 14)} />
      <Bar x={112} y={547} w={380} h={10} color={GREY} p={k(f, b(s, 3) + 20, 14)} />
      <T cx={1140} cy={612} size={17} color={GREY_D} op={chat}>иллюстрация</T>
      <Pill cx={1035} cy={540} w={300} h={34} size={17} text="источник: файл · фрагмент" color={PURPLE} op={k(f, b(s, 3) + 26, 12)} />
    </>
  );
};

// ---------- 7. разработчикам и агентам ----------
const Scene7: React.FC<{f: number}> = ({f}) => {
  const s = 6;
  const ag = k(f, SCENE_FROM[7] - 2, 14);
  const key = k(f, b(s, 1) - 4, 12);
  const gw = k(f, b(s, 2) - 4, 14);
  const pr = k(f, b(s, 2) + 6, 14);
  const roles = k(f, b(s, 3) - 2, 12);
  const rows = [{t: 'читать проект', ok: true}, {t: 'задать вопрос', ok: true}, {t: 'прислать вердикт', ok: true}, {t: 'выполнить команду', ok: false}];
  return (
    <>
      {HDR('ДЛЯ РАЗРАБОТЧИКОВ И АГЕНТОВ', 430, f, SCENE_FROM[7] - 4)}
      <Box x={50} y={225} w={220} h={150} r={16} stroke={CYAN} op={ag} glow={ag > 0.6 ? 'rgba(88,255,238,.3)' : undefined} />
      <T cx={160} cy={277} size={30} family={FONT} weight={800} color={CYAN} op={ag}>ИИ-агент</T>
      <T cx={160} cy={327} size={20} color={GREY} op={ag}>недоверенный</T>
      <Arrow x0={274} y0={300} x1={438} y1={300} p={k(f, b(s, 1) - 8, 14)} color={CYAN} />
      <Pill cx={356} cy={260} w={130} h={34} size={18} text="MCP · ключ" color={PURPLE} op={key} scale={0.9 + 0.1 * key} />
      <Box x={444} y={140} w={340} h={320} r={18} stroke="#fff" op={gw} glow={gw > 0.6 ? 'rgba(102,48,248,.35)' : undefined} />
      <T cx={614} cy={176} size={28} weight={900} op={gw}>Шлюз</T>
      <T cx={614} cy={206} size={17} color={GREY} op={gw}>белый список действий</T>
      {rows.map((r, i) => {
        const a = k(f, b(s, 3) - 6 + i * 8, 12);
        const y = 258 + i * 48;
        return (
          <React.Fragment key={i}>
            <div style={{position: 'absolute', left: 464, top: y - 18, width: 300, height: 38, borderRadius: 10, border: `2px solid ${r.ok ? GREEN : ORANGE}`, opacity: a * gw}} />
            <T cx={600} cy={y + 1} size={19} color={r.ok ? '#fff' : ORANGE} op={a * gw}>{r.t}</T>
            {r.ok ? <Check cx={742} cy={y + 1} p={a} /> : <Check cx={742} cy={y + 1} p={0} />}
            {!r.ok && a > 0 ? <T cx={742} cy={y + 1} size={26} color={ORANGE} op={a}>✕</T> : null}
          </React.Fragment>
        );
      })}
      <Arrow x0={788} y0={300} x1={950} y1={300} p={k(f, b(s, 2) + 2, 14)} />
      <Box x={956} y={225} w={250} h={150} r={16} stroke={PURPLE_L} op={pr} />
      <T cx={1081} cy={300} size={34} weight={900} op={pr}>Проект</T>
      {[{x: 333, t: 'admin', c: ORANGE}, {x: 473, t: 'manager', c: PURPLE_L}, {x: 613, t: 'user', c: CYAN}].map((r, i) => (
        <Pill key={i} cx={r.x} cy={540} w={120} h={38} size={20} text={r.t} color={r.c} op={k(f, b(s, 4) + i * 6, 12)} />
      ))}
      <Pill cx={863} cy={540} w={290} h={38} size={19} text="изоляция проектов" color={GREEN} op={roles} scale={0.9 + 0.1 * roles} />
    </>
  );
};

// ---------- 8. финал ----------
const Scene8: React.FC<{f: number}> = ({f}) => {
  const s = 7;
  const wm = k(f, SCENE_FROM[8] - 2, 20);
  const pills = [{w: 200, t: 'локально', at: b(s, 0)}, {w: 240, t: 'в вашей сети', at: b(s, 1)}, {w: 300, t: 'команда до 20 человек', at: b(s, 2)}];
  const cloudOp = k(f, b(s, 0), 14);
  const breakOp = k(f, b(s, 0) + 10, 10);
  return (
    <>
      <T cx={640} cy={290} size={82} family={FONT_ORB} weight={800} op={wm} glow={`0 0 ${30 * wm}px rgba(102,48,248,.9), 0 0 ${80 * wm}px rgba(102,48,248,.5)`}>Primary Analytics</T>
      <div style={{position: 'absolute', left: 0, top: 377, width: 1280, display: 'flex', justifyContent: 'center', gap: 24}}>
        {pills.map((q, i) => {
          const o = k(f, q.at, 12);
          return <div key={i} style={{width: q.w, height: 46, borderRadius: 23, background: PURPLE, opacity: o, transform: `scale(${0.85 + 0.15 * o})`, display: 'flex', alignItems: 'center', justifyContent: 'center', whiteSpace: 'nowrap', fontFamily: FONT, fontWeight: 800, fontSize: 22, color: '#fff', boxShadow: `0 0 18px ${PURPLE}88`}}>{q.t}</div>;
        })}
      </div>
      <div style={{opacity: cloudOp}}>
        <svg width={1280} height={720} style={{position: 'absolute', left: 0, top: 0}}>
          <g stroke={GREY} strokeWidth={3} fill="#000">
            <ellipse cx={230} cy={545} rx={70} ry={26} />
            <circle cx={195} cy={520} r={22} />
            <circle cx={235} cy={510} r={26} />
            <circle cx={275} cy={522} r={20} />
          </g>
          <line x1={302} y1={540} x2={362} y2={540} stroke={ORANGE} strokeWidth={3} strokeDasharray="8 6" opacity={1 - breakOp * 0.7} />
        </svg>
      </div>
      {breakOp > 0 ? (
        <svg width={1280} height={720} style={{position: 'absolute', left: 0, top: 0, opacity: breakOp}}>
          <polyline points="358,528 374,552 390,528 406,552" fill="none" stroke={ORANGE} strokeWidth={4} strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ) : null}
      <div style={{position: 'absolute', left: 988, top: 518, width: 44, height: 44, borderRadius: 22, background: '#000', border: `3px solid ${PURPLE_L}`, opacity: cloudOp, boxShadow: `0 0 ${18 * cloudOp}px 4px rgba(102,48,248,.6)`}} />
    </>
  );
};

// ---------- субтитры ----------
const stroke = (w: number) => Array.from({length: 16}, (_, i) => `${(Math.cos((i / 16) * 6.283) * w).toFixed(2)}px ${(Math.sin((i / 16) * 6.283) * w).toFixed(2)}px 0 #000`).join(', ');
const Subs: React.FC<{f: number}> = ({f}) => {
  const s = SUBS.find((x) => f >= x.from && f <= x.to);
  if (!s) return null;
  return <div style={{position: 'absolute', left: 640, top: 637, transform: 'translateX(-50%)', whiteSpace: 'nowrap', fontFamily: FONT, fontWeight: 700, fontSize: 40, color: '#fff', lineHeight: 1.2, textShadow: stroke(4)}}>{s.text}</div>;
};

export const Video: React.FC = () => {
  const f = useCurrentFrame() + 1;
  const scenes = [Scene1, Scene2, Scene3, Scene4, Scene5, Scene6, Scene7, Scene8];
  return (
    <AbsoluteFill style={{background: '#000'}}>
      <Fonts />
      <Audio src={staticFile('assets/audio.wav')} />
      <Dots f={f} />
      {scenes.map((S, i) => {
        const a = SCENE_FROM[i + 1] - 12;
        const last = i === scenes.length - 1;
        const e = last ? TOTAL_FRAMES + 1 : SCENE_FROM[i + 2] - 3;
        if (f < a || f > e) return null;
        const op = k(f, a, 10) * (last ? 1 : 1 - k(f, e - 6, 6));
        return <AbsoluteFill key={i} style={{opacity: op}}><S f={f} /></AbsoluteFill>;
      })}
      <Subs f={f} />
    </AbsoluteFill>
  );
};
