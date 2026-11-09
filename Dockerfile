# syntax=docker/dockerfile:1
#
# fieldsim as a container, e.g. a service in CI or docker compose:
#
#   docker run --rm -p 5020:5020 -v "$PWD/devices.yaml:/config/devices.yaml:ro" \
#       ghcr.io/enesuslu15/fieldsim
#
# The image runs `fieldsim run /config/devices.yaml --host 0.0.0.0`; pass other
# arguments to override, e.g. `... ghcr.io/enesuslu15/fieldsim validate /config/x.yaml`.

FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.12-slim
LABEL org.opencontainers.image.title="fieldsim" \
      org.opencontainers.image.description="Virtual Modbus and OPC UA devices for testing industrial software" \
      org.opencontainers.image.source="https://github.com/enesuslu15/fieldsim" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY --from=build /wheels /wheels
RUN wheel="$(ls /wheels/fieldsim-*.whl)" \
    && pip install --no-cache-dir "${wheel}[opcua]" \
    && rm -rf /wheels \
    && useradd --system --uid 10001 --no-create-home fieldsim

COPY examples/boiler.yaml /config/devices.yaml

USER fieldsim
WORKDIR /config
EXPOSE 502 4840 5020

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["fieldsim", "check", "/config/devices.yaml"]

ENTRYPOINT ["fieldsim"]
CMD ["run", "/config/devices.yaml", "--host", "0.0.0.0"]
