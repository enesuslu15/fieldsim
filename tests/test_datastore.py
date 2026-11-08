import pytest

from fieldsim.config import RegisterMap
from fieldsim.core.datastore import PointNotFoundError
from fieldsim.core.types import encode
from fieldsim.protocols.modbus import ExceptionCode, ModbusDataStore, ModbusError, Table

from .conftest import modbus_spec


@pytest.fixture
def store():
    spec = modbus_spec()
    return ModbusDataStore(spec.registers, unmapped=spec.unmapped)


def test_initial_values(store):
    assert store.get("pump") is True
    assert store.get("valve") is False
    assert store.get("door") is True
    assert store.get("setpoint") == 75
    assert store.get("temperature") == pytest.approx(21.5)
    assert store.get("counter") == 70000


def test_raw_view_matches_points(store):
    assert store.read_registers(Table.HOLDING_REGISTERS, 1, 2) == encode(21.5, "float32")
    assert store.read_registers(Table.INPUT_REGISTERS, 0, 2) == [1, 70000 - 65536]
    assert store.read_bits(Table.COILS, 0, 3) == [True, False, False]


def test_set_and_get(store):
    store.set("temperature", 99.25)
    assert store.get("temperature") == 99.25
    store.set("valve", 1)
    assert store.get("valve") is True


def test_set_validates_unless_clamping(store):
    with pytest.raises(ValueError, match="out of range"):
        store.set("setpoint", -1)
    store.set("setpoint", -1, clamp=True)
    assert store.get("setpoint") == 0


def test_unknown_point(store):
    with pytest.raises(PointNotFoundError) as info:
        store.get("nope")
    assert "no point named 'nope'; available: counter, door" in str(info.value)


def test_point_layout(store):
    layout = store.point("temperature")
    assert layout.table is Table.HOLDING_REGISTERS
    assert layout.addresses == range(1, 3)
    assert {p.name for p in store.points()} == set(store.names())


def test_partial_write_of_multi_register_point(store):
    hi, lo = encode(-2.0, "float32")
    store.write_registers(1, [hi])
    store.write_registers(2, [lo])
    assert store.get("temperature") == -2.0


def test_unmapped_read_raises_illegal_address(store):
    with pytest.raises(ModbusError) as info:
        store.read_registers(Table.HOLDING_REGISTERS, 3, 2)
    assert info.value.code is ExceptionCode.ILLEGAL_DATA_ADDRESS


def test_read_only_write_raises_illegal_address(store):
    with pytest.raises(ModbusError, match="read-only"):
        store.write_registers(3, [1])
    with pytest.raises(ModbusError, match="read-only"):
        store.write_coils(2, [True])


def test_failed_write_is_atomic(store):
    with pytest.raises(ModbusError):
        store.write_registers(0, [1, 2, 3, 4, 5])  # 4 is unmapped
    assert store.get("setpoint") == 75


def test_unmapped_allow_acts_as_memory():
    store = ModbusDataStore(RegisterMap(), unmapped="allow")
    assert store.read_registers(Table.HOLDING_REGISTERS, 100, 3) == [0, 0, 0]
    store.write_registers(100, [7, 0x1FFFF])
    assert store.read_registers(Table.HOLDING_REGISTERS, 100, 2) == [7, 0xFFFF]
    store.write_coils(5, [True])
    assert store.read_bits(Table.COILS, 4, 2) == [False, True]


def test_wrong_table_kind(store):
    with pytest.raises(ValueError, match="not a bit table"):
        store.read_bits(Table.HOLDING_REGISTERS, 0, 1)
    with pytest.raises(ValueError, match="not a register table"):
        store.read_registers(Table.COILS, 0, 1)


def test_write_listeners(store):
    events = []
    unsubscribe = store.add_write_listener(events.append)
    store.write_registers(0, [1, 2, 3])
    store.write_coils(0, [False, True])
    store.set("setpoint", 5)  # API writes are not client writes
    unsubscribe()
    store.write_registers(0, [9])
    assert len(events) == 2
    assert events[0].table is Table.HOLDING_REGISTERS
    assert events[0].values == (1, 2, 3)
    assert events[0].points == ("setpoint", "temperature")
    assert events[1].values == (False, True)
    assert events[1].points == ("pump", "valve")
