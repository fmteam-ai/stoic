# Base image is tag-pinned; scripts/pin_image_digests.sh rewrites it to
# tag@sha256 (needs registry access) — CI reports unpinned images.
FROM python:3.11-slim
WORKDIR /srv
COPY signer-requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system --gid 10001 signer \
    && useradd --system --uid 10001 --gid signer --home-dir /nonexistent --shell /usr/sbin/nologin signer
COPY app.py .
COPY entrypoint.sh /usr/local/bin/signer-entrypoint
RUN chmod 0755 /usr/local/bin/signer-entrypoint
# The service runs as uid 10001 (entrypoint drops root after staging Docker
# secrets it cannot otherwise read). Inside a container 0.0.0.0 is the
# container's private interface only: compose publishes NO port and attaches
# the signer to the internal-only `signer_net` network (API/workers only).
# SIGNER_BIND_HOST narrows it further where the platform allows.
ENV SIGNER_BIND_HOST=0.0.0.0
EXPOSE 9443
ENTRYPOINT ["/usr/local/bin/signer-entrypoint"]
CMD ["sh", "-c", "exec uvicorn app:app --host \"${SIGNER_BIND_HOST}\" --port 9443"]
