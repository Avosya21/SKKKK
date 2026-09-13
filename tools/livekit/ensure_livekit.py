"""CLI: write LiveKit keys into .env and tools/livekit/livekit.yaml."""

from __future__ import annotations

from app.app.services.livekit_env import ensure_livekit_files

if __name__ == "__main__":
    ensure_livekit_files()
    print("livekit.yaml written")
    print("LIVEKIT_URL set")
