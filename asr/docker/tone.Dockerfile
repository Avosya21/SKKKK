# Официальный образ T-one (модели уже внутри /models).
# Добавляем POST /transcribe и GET /health для PrimaryAnalitics.
FROM tinkoffcreditsystems/t-one:0.1.0

USER root
COPY docker/tone_rest.py /opt/primaryanalitics/tone_rest.py
RUN chown -R python:python /opt/primaryanalitics

USER python
WORKDIR /opt/primaryanalitics
ENV PYTHONPATH=/opt/primaryanalitics
ENV LOAD_FROM_FOLDER=/models
ENV ASR_ENGINE=tone
EXPOSE 8080

ENTRYPOINT ["uvicorn", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "tone_rest:app"]
