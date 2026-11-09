"""The plugin is tested the way users meet it: in a separate pytest run."""

import textwrap

import pytest

pytest_plugins = ["pytester"]

HELPERS = textwrap.dedent(
    """
    import socket, struct

    def read_hr(handle, address=0):
        with socket.create_connection(handle.address, timeout=2) as s:
            s.sendall(struct.pack(">HHHBBHH", 1, 0, 6, handle.unit_id, 3, address, 1))
            return struct.unpack(">H", s.recv(64)[-2:])[0]
    """
)

PLANT = textwrap.dedent(
    """
    devices:
      - name: boiler
        protocol: modbus_tcp
        port: 5020
        registers:
          holding_registers:
            - {name: temperature, address: 0, value: 60}
        scenario:
          - {at: 10s, set: {temperature: 95}}
      - name: pump
        protocol: modbus_rtu_over_tcp
        port: 5021
        unit_id: 4
    """
)


@pytest.fixture
def project(pytester):
    pytester.makepyfile(helpers=HELPERS)
    pytester.makefile(".yaml", plant=PLANT)
    pytester.makeini(BASE_INI)
    return pytester


BASE_INI = "[pytest]\nasyncio_default_fixture_loop_scope = function\n"


def test_zero_config_device(project):
    project.makepyfile(
        """
        from helpers import read_hr

        def test_it(modbus_device):
            assert modbus_device.protocol == "modbus_tcp"
            assert read_hr(modbus_device, 1234) == 0  # whole address space is memory
        """
    )
    project.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1)


def test_marker_with_yaml_and_device_choice(project):
    project.makepyfile(
        """
        import pytest
        from helpers import read_hr

        @pytest.mark.fieldsim("plant.yaml", device="boiler")
        def test_boiler(modbus_device):
            assert modbus_device.port != 5020  # free ports by default
            assert read_hr(modbus_device) == 60

        @pytest.mark.fieldsim("plant.yaml", device="pump")
        def test_pump(modbus_device):
            assert modbus_device.protocol == "modbus_rtu_over_tcp"

        @pytest.mark.fieldsim("plant.yaml")
        def test_all(fieldsim):
            assert set(fieldsim.devices) == {"boiler", "pump"}
        """
    )
    project.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=3)


def test_ini_options_and_virtual_clock(project):
    project.makeini(BASE_INI + "fieldsim_config = plant.yaml\nfieldsim_clock = virtual\n")
    project.makepyfile(
        """
        import pytest

        @pytest.mark.fieldsim(device="boiler")
        def test_scenario(modbus_device):
            modbus_device.advance(10)
            assert modbus_device.get("temperature") == 95
        """
    )
    project.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1)


def test_dict_config_and_fixed_ports(project):
    project.makepyfile(
        """
        import socket, pytest

        def free_port():
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]

        PORT = free_port()

        @pytest.mark.fieldsim(
            {"devices": [{"name": "x", "protocol": "modbus_tcp", "port": PORT}]},
            free_ports=False,
        )
        def test_fixed(modbus_device):
            assert modbus_device.port == PORT
        """
    )
    project.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1)


def test_make_modbus_device_factory(project):
    project.makepyfile(
        """
        from helpers import read_hr

        def test_two(make_modbus_device):
            a = make_modbus_device(
                registers={"holding_registers": [{"name": "x", "address": 0, "value": 7}]}
            )
            b = make_modbus_device({"unit_id": 3}, clock="virtual")
            assert a.name == "device1" and b.name == "device2"
            assert a.port != b.port
            assert read_hr(a) == 7
            b.advance(1)
        """
    )
    project.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1)


def test_async_test_with_plugin_fixture(project):
    project.makepyfile(
        """
        import asyncio
        import pytest

        @pytest.mark.asyncio
        async def test_async(modbus_device):
            reader, writer = await asyncio.open_connection(*modbus_device.address)
            writer.write(bytes.fromhex("0001000000060106000a0005"))
            await reader.read(12)
            writer.close()
            event = await modbus_device.wait_for_write_async(timeout=2)
            assert event.values == (5,)
        """
    )
    project.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1)


@pytest.mark.parametrize(
    ("test_source", "message"),
    [
        (
            """
            import pytest

            @pytest.mark.fieldsim("plant.yaml")
            def test_it(modbus_device):
                pass
            """,
            "needs exactly one Modbus device, found: boiler, pump",
        ),
        (
            """
            import pytest

            @pytest.mark.fieldsim("missing.yaml")
            def test_it(fieldsim):
                pass
            """,
            "fieldsim: *missing.yaml: cannot read file",
        ),
        (
            """
            import pytest

            @pytest.mark.fieldsim({"devices": []})
            def test_it(fieldsim):
                pass
            """,
            "fieldsim: invalid configuration",
        ),
        (
            """
            import socket, pytest

            blocker = socket.socket()
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            PORT = blocker.getsockname()[1]

            @pytest.mark.fieldsim(
                {"devices": [{"name": "x", "protocol": "modbus_tcp", "port": PORT}]},
                free_ports=False,
            )
            def test_it(fieldsim):
                pass
            """,
            "fieldsim: cannot start devices",
        ),
    ],
)
def test_setup_errors_are_reported_cleanly(project, test_source, message):
    project.makepyfile(textwrap.dedent(test_source))
    result = project.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines([f"*{message}*"])


def test_marker_is_registered(project):
    result = project.runpytest("--markers")
    result.stdout.fnmatch_lines(["@pytest.mark.fieldsim(config=None, *, device=None*"])


LINE = textwrap.dedent(
    """
    devices:
      - name: line
        protocol: opcua
        port: 4840
        nodes:
          - {name: Speed, type: Double, value: 1.25, writable: true}
    """
)


def test_opcua_fixtures(project):
    project.makefile(".yaml", line=LINE)
    project.makepyfile(
        """
        import pytest
        from asyncua import Client

        async def read(handle, point):
            async with Client(handle.endpoint, timeout=2) as client:
                return await client.get_node(handle.node_id(point)).read_value()

        @pytest.mark.fieldsim("line.yaml")
        @pytest.mark.asyncio
        async def test_from_file(opcua_device):
            assert opcua_device.port != 4840
            assert await read(opcua_device, "Speed") == 1.25

        def test_zero_config(opcua_device):
            assert opcua_device.endpoint.startswith("opc.tcp://")
            assert opcua_device.protocol == "opcua"

        @pytest.mark.asyncio
        async def test_factory(make_opcua_device):
            line = make_opcua_device(nodes=[{"name": "X", "type": "Int32", "value": 3}])
            assert await read(line, "X") == 3

        @pytest.mark.fieldsim("plant.yaml")
        def test_no_opcua_device(opcua_device):
            pass
        """
    )
    result = project.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=3, errors=1)
    result.stdout.fnmatch_lines(["*opcua_device needs exactly one OPC UA device, found: none*"])
