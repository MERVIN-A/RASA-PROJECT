# Rasa server image (NLU + Core). Trains the model at build time so the container is self-contained.
FROM rasa/rasa:3.6.21-full
WORKDIR /app
COPY --chown=1001 . /app
USER root
RUN pip install --no-cache-dir https://github.com/explosion/spacy-models/releases/download/en_core_web_md-3.4.1/en_core_web_md-3.4.1-py3-none-any.whl
USER 1001
RUN rasa train --fixed-model-name eco-advisor
EXPOSE 5005
# --enable-api + token: protected HTTP API; CORS restricted via env var
CMD ["run", "--endpoints", "endpoints.docker.yml", "--enable-api", "--auth-token", "${RASA_TOKEN}", "--cors", "${ALLOWED_ORIGIN}", "--port", "5005"]
