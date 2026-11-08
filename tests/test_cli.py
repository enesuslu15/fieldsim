import json
import socket
import subprocess
import sys
import textwrap
import time

import pytest
from typer.testing import CliRunner

from fieldsim import __version__
from fieldsim.cli import app

runner = CliRunner()

VALID = textwrap.dedent(
    """
    devices:
      - name: plc
        protocol: modbus_tcp
        port: 0
        registers:
          holding_registers:
            - {name: x, address: 0, generator: {type: sine, min: 0, max: 10, period: 1s}}
    """
)


@pytest.fixture
def valid_file(tmp_path):
    path = tmp_path / "ok.yaml"
    path.write_text(VALID, encoding="utf-8")
    return path


@pytest.fixture
def invalid_file(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("devices:\n  - {name: plc, protocol: modbus_tcp, port: -1}\n", encoding="utf-8")
    return path


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_no_args_shows_help():
    result = runner.invoke(app, [])
    assert "validate" in result.output


def test_validate_ok(valid_file):
    result = runner.invoke(app, ["validate", str(valid_file)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert "1 device, 1 point" in result.output


def test_validate_reports_errors_and_fails(valid_file, invalid_file):
    result = runner.invoke(app, ["validate", str(valid_file), str(invalid_file)])
    assert result.exit_code == 1
    assert "devices[0].port" in result.output


def test_schema_stdout():
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0
    assert json.loads(result.output)["title"] == "fieldsim simulation file"


def test_schema_to_file(tmp_path):
    out = tmp_path / "schema.json"
    result = runner.invoke(app, ["schema", "-o", str(out)])
    assert result.exit_code == 0
    assert "devices" in json.loads(out.read_text(encoding="utf-8"))["properties"]


def test_run_with_duration(valid_file):
    result = runner.invoke(app, ["run", str(valid_file), "--duration", "0.2"])
    assert result.exit_code == 0, result.output
    assert "plc" in result.output
    assert "modbus_tcp" in result.output
    assert "(1 points, 1 generated)" in result.output
    assert "stopped" in result.output


def test_run_invalid_file(invalid_file):
    result = runner.invoke(app, ["run", str(invalid_file)])
    assert result.exit_code == 1


def test_run_bad_log_level(valid_file):
    result = runner.invoke(app, ["run", str(valid_file), "--log-level", "loud"])
    assert result.exit_code != 0
    assert "unknown log level" in result.output


def test_run_port_in_use(tmp_path):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        path = tmp_path / "clash.yaml"
        path.write_text(
            f"devices:\n  - {{name: plc, protocol: modbus_tcp, port: {port}}}\n", encoding="utf-8"
        )
        result = runner.invoke(app, ["run", str(path), "--duration", "0.1"])
    assert result.exit_code == 1
    assert "cannot start devices" in result.output


def test_console_script_serves_until_terminated(tmp_path):
    """Run the real CLI in a subprocess and talk to it over TCP."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    path = tmp_path / "sim.yaml"
    path.write_text(
        "devices:\n  - name: plc\n    protocol: modbus_tcp\n"
        f"    port: {port}\n    registers:\n"
        "      holding_registers: [{name: x, address: 0, value: 42}]\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "fieldsim", "run", str(path), "--duration", "10"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                conn = socket.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise
                time.sleep(0.05)
        with conn:
            conn.sendall(b"\x00\x01\x00\x00\x00\x06\x01\x03\x00\x00\x00\x01")
            assert conn.recv(64) == b"\x00\x01\x00\x00\x00\x05\x01\x03\x02\x00\x2a"
    finally:
        proc.kill()
        proc.wait(timeout=5)
