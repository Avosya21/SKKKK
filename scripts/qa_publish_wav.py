"""Публикует wav-файл как микрофон участника в комнату LiveKit (для проверки записи и анализа без людей).
Запуск в venv с livekit:  venvs\voice\Scripts\python.exe scripts\qa_publish_wav.py <token> <file.wav> [задержка_с]
Токен выдаёт код приложения (services.meetings.join_guest / livekit_join); URL берётся из LIVEKIT_RTC_INTERNAL (по умолчанию ws://127.0.0.1:7880)."""
import asyncio
import os
import sys
import time
import wave

import numpy as np
from livekit import rtc

TOKEN, PATH = sys.argv[1], sys.argv[2]
DELAY = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
URL = os.environ.get("LIVEKIT_RTC_INTERNAL", "ws://127.0.0.1:7880")
RATE, FRAME = 48000, 480  # 10 мс


def load_48k(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16 if width == 2 else np.uint8)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if rate != RATE:
        x = np.arange(len(data)) / rate
        xn = np.arange(int(len(data) * RATE / rate)) / RATE
        data = np.interp(xn, x, data).astype(np.int16)
    return data


async def main() -> None:
    pcm = load_48k(PATH)
    await asyncio.sleep(DELAY)
    room = rtc.Room()
    await room.connect(URL, TOKEN)
    source = rtc.AudioSource(RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("voice", source)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    print("published", round(len(pcm) / RATE, 1), "s", flush=True)
    for i in range(0, len(pcm) - FRAME + 1, FRAME):
        await source.capture_frame(rtc.AudioFrame(pcm[i : i + FRAME].tobytes(), RATE, 1, FRAME))
    await asyncio.sleep(1.5)
    await room.disconnect()
    print("done", flush=True)


asyncio.run(main())
