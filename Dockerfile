FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MURASAME_CONTAINER_BIND=1
WORKDIR /opt/murasame
COPY pyproject.toml run.py config.yml ./
COPY src ./src
COPY character/settings.yml character/text_profile.yml ./character/
COPY character/avatar/dashboard-smile.jpg character/avatar/dashboard-letter.jpg character/avatar/dashboard-kimono.png ./character/avatar/
COPY deploy/container_entrypoint.py ./deploy/container_entrypoint.py
RUN pip install --no-cache-dir '.[login]' && \
    groupadd --gid 10001 murasame && useradd --uid 10001 --gid 10001 --no-create-home murasame && \
    mkdir /workspace && chown 10001:10001 /workspace
USER 10001:10001
WORKDIR /workspace
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/', timeout=2).close()"
ENTRYPOINT ["python", "/opt/murasame/deploy/container_entrypoint.py"]
CMD ["serve"]
