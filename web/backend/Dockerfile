FROM docker:27-cli AS docker-cli

FROM python:3.12-slim

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

RUN apt-get update && apt-get install -y --no-install-recommends \
    android-tools-adb sudo gcc libc6-dev xz-utils && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .
COPY redroid-script /opt/virtuale/redroid-script
COPY tools /opt/virtuale/tools

RUN pip install --no-cache-dir -r /opt/virtuale/redroid-script/requirements.txt

RUN gcc -O2 -static -o /opt/virtuale/tools/create_touch /opt/virtuale/tools/create_touch.c && \
    gcc -O2 -static -o /opt/virtuale/tools/create_keyboard /opt/virtuale/tools/create_keyboard.c && \
    gcc -O2 -static -o /opt/virtuale/tools/touch_inject /opt/virtuale/tools/touch_inject.c && \
    gcc -O2 -static -o /opt/virtuale/tools/init_wrapper /opt/virtuale/tools/init_wrapper.c

RUN python manage.py collectstatic --noinput 2>/dev/null || true

COPY backend/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
