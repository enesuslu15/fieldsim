# Docker and CI

## The image

`ghcr.io/enesuslu15/fieldsim` (linux/amd64 and linux/arm64) contains fieldsim
with OPC UA and S7 support. It runs

```bash
fieldsim run /config/devices.yaml --host 0.0.0.0
```

as an unprivileged user and has a health check (`fieldsim check`).

```bash
docker run --rm -p 5020:5020 \
  -v "$PWD/boiler.yaml:/config/devices.yaml:ro" \
  ghcr.io/enesuslu15/fieldsim:latest
```

Tags: `latest`, `X.Y.Z`, `X.Y` for releases and `edge` for the main branch.

## docker compose

```yaml
services:
  plant:
    image: ghcr.io/enesuslu15/fieldsim:latest
    volumes: ["./boiler.yaml:/config/devices.yaml:ro"]
    ports: ["5020:5020"]

  gateway:
    build: .
    environment: { MODBUS_HOST: plant, MODBUS_PORT: "5020" }
    depends_on:
      plant: { condition: service_healthy }
```

## GitHub Actions

With the pytest plugin there is nothing to start — install and run your tests:

```yaml
- uses: actions/setup-python@v7
  with: { python-version: "3.12" }
- run: pip install "fieldsim[all]" -e .
- run: pytest
```

For tests that are not written in Python, or an end-to-end run of the real
application, start the container and wait for it:

```yaml
- name: Start the simulated plant
  run: |
    docker run -d --name plant -p 5020:5020 \
      -v "$PWD/tests/devices.yaml:/config/devices.yaml:ro" \
      ghcr.io/enesuslu15/fieldsim:latest
    docker run --rm --network host \
      -v "$PWD/tests/devices.yaml:/config/devices.yaml:ro" \
      ghcr.io/enesuslu15/fieldsim:latest check /config/devices.yaml --wait 30
- run: npm test
- if: always()
  run: docker logs plant
```

A complete workflow is in
[`examples/github-actions.yml`](https://github.com/enesuslu15/fieldsim/blob/main/examples/github-actions.yml).

## Coverage

Measure your tests with `coverage run -m pytest` rather than `pytest --cov`
if you want fieldsim's own lines counted too; for your code either works.
