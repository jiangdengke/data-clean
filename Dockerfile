FROM node:22-alpine AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/src ./src
COPY frontend/index.html ./index.html
RUN npm ci
RUN npm run build

FROM python:3.12-slim AS rclone-download
ARG TARGETARCH
ARG RCLONE_VERSION=1.74.3
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) RCLONE_SHA256="dbee7ccd7a5d617e4ed4cd4555c16669b511abfe8d31164f61be35ac9e999bd2" ;; \
        arm64) RCLONE_SHA256="8f8d47446e061f80c3256659fe8e21f56d72d96aaefe1275d088ea5eb6b42aa7" ;; \
        *) echo "Unsupported rclone architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install --no-install-recommends -y ca-certificates curl unzip; \
    curl -fsSLo /tmp/rclone.zip "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}.zip"; \
    echo "${RCLONE_SHA256}  /tmp/rclone.zip" | sha256sum -c -; \
    unzip -q /tmp/rclone.zip -d /tmp/rclone; \
    install -D -m 0755 "/tmp/rclone/rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}/rclone" /out/rclone

FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=rclone-download /out/rclone /usr/local/bin/rclone
COPY backend ./backend
COPY --from=frontend-build /frontend/dist ./frontend/dist
RUN mkdir -p /var/lib/r2-model-scanner
VOLUME ["/var/lib/r2-model-scanner"]
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
