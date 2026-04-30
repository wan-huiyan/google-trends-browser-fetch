# Cloud Run: Playwright needs browser binaries + OS libs; buildpacks only run pip.
# Browsers live in a world-readable path so the non-root Cloud Run user can execute them.
FROM python:3.12-bookworm

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PLAYWRIGHT_SKIP_RUNTIME_INSTALL=1

WORKDIR /workspace

COPY requirements.txt .
RUN mkdir -p /opt/ms-playwright \
    && pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && chmod -R 755 /opt/ms-playwright

COPY *.py project.toml ./

EXPOSE 8080
CMD sh -c 'exec functions-framework --target=run_trends_job --host=0.0.0.0 --port=${PORT:-8080}'
