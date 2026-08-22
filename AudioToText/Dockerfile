# Base image with CUDA + PyTorch (GPU support)
FROM pytorch/pytorch:2.3.0-cuda11.8-cudnn8-runtime

# System deps: ffmpeg for audio extraction, locales for UTF-8
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy your script into the image
# Rename this if файл у тебя называется иначе
COPY . .

RUN pip install -r requirements.txt

# Optional: cache dir for HF models inside container
ENV TRANSFORMERS_CACHE=/app/hf_cache
ENV HF_HOME=/app/hf_cache

ENTRYPOINT [ "python", "main.py" ]