"""Recovery on one PACE master cable; no battery write commands."""
import asyncio
import os
import pty
import threading
from unittest.mock import Mock

import pytest
import paho.mqtt.client as paho
import bmslib.wired as wired
import bmslib.mqtt_util as mqtt
from bmslib.models.pace import PaceUart, build_frame, parse_frame
from bmslib.test.test_pace_decode import ANALOG_RESP, STATUS_RESP
from bmslib.sampling import BmsSampler


@pytest.fixture(autouse=True)
def isolate():
    wired._reset_shared_ports()
    mqtt._last_values.clear()
    mqtt._discovery.clear()
    mqtt._availability_topics.clear()
    yield
    wired._reset_shared_ports()


def pack(n, path):
    return PaceUart('serial', name=f'taico{n}', adapter=str(path), type_spec=str(n))


def test_missing_cable_never_leaks_users(tmp_path):
    b = pack(1, tmp_path / 'missing')
    async def scenario():
        for _ in range(30):
            with pytest.raises(OSError):
                await b.connect()
            await b.disconnect()  # safe even before first notify
            assert b.client.port.users == 0
    asyncio.run(scenario())


def test_sampler_outage_backoff_and_other_pack_progress(tmp_path):
    a, b = [BmsSampler(pack(n, tmp_path / 'missing'), mqtt_client=None,
                       dt_max_seconds=120, expire_after_seconds=20) for n in (1, 2)]
    calls = []
    async def failed():
        calls.append('a')
        raise OSError('cable absent')
    async def healthy():
        calls.append('b')
        return True
    a._sample_inner, b._sample_inner = failed, healthy
    async def scenario():
        for failure in range(10):
            a._time_next_retry = 0
            assert await a() is None
            assert await b()
            assert 0 < a._time_next_retry - __import__('time').time() <= 60
            assert await a() is None  # waiting cycles do not retry or inflate backoff
        assert calls.count('a') == calls.count('b') == 10
        a._sample_inner = healthy
        a._time_next_retry = 0
        assert await a()
        assert a._serial_failures == 0
    asyncio.run(scenario())


@pytest.mark.skipif(__import__('sys').platform == 'darwin', reason='macOS pty close blocks with concurrent reads; Linux CI covers real tty')
def test_pace_two_packs_real_serial_unplug_replug(tmp_path):
    link = tmp_path / 'battery'
    a, b, absent = [pack(n, link) for n in (1, 2, 3)]
    for obj in (a, b, absent):
        obj.TIMEOUT = 0.25
    commands = []
    devices = []

    def cable():
        master, slave = pty.openpty()
        stop = threading.Event()
        devices.append((master, slave, stop))
        link.symlink_to(os.ttyname(slave))
        def sim():
            buf = bytearray()
            while not stop.is_set():
                try:
                    buf.extend(os.read(master, 512))
                except OSError:
                    return
                while b'\r' in buf:
                    frame, _, rest = buf.partition(b'\r')
                    buf[:] = rest
                    if not frame.startswith(b'~'):
                        continue
                    fields = parse_frame(bytes(frame) + b'\r')
                    commands.append(fields)
                    n = int(fields['info'], 16)
                    assert fields['adr'] == n
                    if n == 3:
                        continue
                    fixture = ANALOG_RESP if fields['cid2'] == 0x42 else STATUS_RESP
                    info = parse_frame(fixture)['info']
                    info = info[:2] + b'%02X' % n + info[4:]
                    os.write(master, build_frame(0x25, 1, 0x46, 0, info))
        threading.Thread(target=sim, daemon=True).start()

    async def scenario():
        cable()
        await a.connect()
        await b.connect()
        assert a.client.port is b.client.port
        assert a.client.bus_lock() is b.client.bus_lock()
        samples = await asyncio.gather(a.fetch(), b.fetch())
        assert all(s.voltage > 0 for s in samples)
        await absent.connect()
        with pytest.raises(TimeoutError):
            await absent.fetch()
        await absent.disconnect()
        assert (await b.fetch()).voltage > 0
        # Actual tty hangup, then a new tty under the same stable by-id path.
        link.unlink()
        master, slave, stop = devices[0]
        stop.set()
        # macOS blocks closing a pty master while a slave read is in flight.
        # Inject the USB I/O failure, then close the old pty after cancellation.
        original_read = a.client.port.t.read
        def unplugged_read():
            raise OSError('USB device removed')
        a.client.port.t.ser.cancel_read()
        a.client.port.t.read = unplugged_read
        for _ in range(40):
            if not a.client.is_connected:
                break
            await asyncio.sleep(0.05)
        assert not a.client.is_connected
        assert not b.client.is_connected
        await a.disconnect()
        await b.disconnect()
        assert a.client.port.users == 0
        os.close(master)
        devices[0] = (None, slave, stop)
        a.client.port.t.read = original_read
        cable()
        await a.connect()
        await b.connect()
        assert len(a.client.port.callback) == 2
        assert (await a.fetch()).voltage > 0
        assert (await b.fetch()).voltage > 0
        await a.disconnect()
        assert b.client.is_connected
        await b.disconnect()
        assert a.client.port.users == 0
    try:
        asyncio.run(asyncio.wait_for(scenario(), 10))
    finally:
        wired._reset_shared_ports()
        for master, slave, stop in devices:
            stop.set()
            if master is not None:
                os.close(master)
            os.close(slave)
    assert commands
    assert {f['cid2'] for f in commands} == {0x42, 0x44}
    assert {int(f['info'], 16) for f in commands} == {1, 2, 3}


def test_status_from_other_pack_is_not_used(tmp_path):
    b = pack(2, tmp_path / 'missing')
    async def read(cid):
        fixture = ANALOG_RESP if cid == 0x42 else STATUS_RESP
        info = parse_frame(fixture)['info']
        if cid == 0x42:
            info = info[:2] + b'02' + info[4:]
        return {'info': info}
    b._read = read
    s = asyncio.run(b.fetch())
    assert s.switches is None and s.problem is None


def test_mqtt_retained_discovery_replay_and_offline():
    client = Mock()
    client.publish.return_value.rc = paho.MQTT_ERR_SUCCESS
    mqtt.register_availability(client, 'taico1')
    from bmslib.bms import BmsSample
    s = BmsSample(voltage=52, current=1, switches={'charge': True})
    mqtt.publish_hass_discovery(client, 'taico1', 20, s, 16, [], read_only=True)
    import json
    for topic, payload in mqtt._discovery.items():
        d = json.loads(payload)
        assert d['availability_mode'] == 'all'
        assert d['availability'][1]['topic'] == 'taico1/availability'
        assert d['unique_id'].startswith('taico1__')
        assert 'command_topic' not in d
        assert not topic.startswith('homeassistant/switch/')
    assert all(c.kwargs.get('retain') for c in client.publish.call_args_list)
    mqtt.mqtt_reconnected(client)
    client.publish.assert_any_call('taico1/availability', 'offline', qos=1, retain=True)
    assert all(c.kwargs.get('retain') for c in client.publish.call_args_list)


def test_two_pace_packs_hotplug_simulation(monkeypatch):
    class Cable:
        def __init__(self, port, **kw):
            self.port, self.is_open, self.available = port, False, False
            self.rx_bytes = 0
            self.commands = []
        def open(self):
            if not self.available:
                raise OSError('cable absent')
            self.is_open = True
        def close(self):
            self.is_open = False
        def read(self):
            if not self.available:
                raise OSError('cable removed')
        def write(self, data):
            if not self.available:
                raise OSError('cable removed')
            fields = parse_frame(data)
            self.commands.append(fields)
            n = int(fields['info'], 16)
            assert fields['adr'] == n
            if n == 3:
                return
            assert fields['cid2'] in (0x42, 0x44)
            fixture = ANALOG_RESP if fields['cid2'] == 0x42 else STATUS_RESP
            info = parse_frame(fixture)['info']
            reply = build_frame(0x25, 1, 0x46, 0, info[:2] + b'%02X' % n + info[4:])
            asyncio.get_running_loop().call_soon(a.client.port._deliver, reply)
    monkeypatch.setattr(wired, 'SerialTransport', Cable)
    a, b, absent = [pack(n, '/dev/by-id/taico') for n in (1, 2, 3)]
    cable = a.client.t
    absent.TIMEOUT = .01
    async def scenario():
        with pytest.raises(OSError):
            await a.connect()
        assert a.client.port.users == 0
        cable.available = True
        await a.connect()
        await b.connect()
        assert a.client.bus_lock() is b.client.bus_lock()
        await asyncio.gather(a.fetch(), b.fetch())
        await absent.connect()
        with pytest.raises(TimeoutError):
            await absent.fetch()
        await absent.disconnect()
        assert (await b.fetch()).voltage > 0
        cable.available = False
        with pytest.raises(OSError):
            await a.fetch()
        assert not b.client.is_connected
        await a.disconnect()
        await b.disconnect()
        cable.available = True
        await a.connect()
        await b.connect()
        assert len(a.client.port.callback) == 2
        await asyncio.gather(a.fetch(), b.fetch())
        await a.disconnect()
        assert b.client.is_connected
        await b.disconnect()
        assert a.client.port.users == 0
    asyncio.run(asyncio.wait_for(scenario(), 5))
    assert {f['cid2'] for f in cable.commands} == {0x42, 0x44}


def test_missing_mqtt_publications_do_not_trigger_process_watchdog(monkeypatch):
    import main
    monkeypatch.setattr(main, 'mqtt_configured', True)
    monkeypatch.setattr(main, 'shutdown', False)
    monkeypatch.setattr(main, 'store_states', lambda samplers: None)
    assert main.bg_checks([], timeout=300, t_start=__import__('time').time() - 10000)
    assert main.shutdown is False


def test_process_stays_alive_without_cable_or_broker_and_stops_cleanly(tmp_path):
    import json
    import subprocess
    import sys
    import time
    from pathlib import Path
    (tmp_path / 'options.json').write_text(json.dumps({
        'devices': [{'address': 'serial', 'adapter': str(tmp_path / 'missing'),
                     'type': f'pace_uart:{n}', 'alias': f'taico{n}'} for n in (1, 2)],
        'mqtt_broker': '127.0.0.1', 'mqtt_port': 1,
        'watchdog': True, 'telemetry': False, 'gui': False,
        'sample_period': .05, 'publish_period': .05, 'keep_alive': True,
    }))
    entry = Path(__file__).resolve().parents[2] / 'main.py'
    with (tmp_path / 'process.log').open('w+') as log:
        proc = subprocess.Popen([sys.executable, str(entry)], cwd=tmp_path,
                                stdout=log, stderr=subprocess.STDOUT)
        try:
            time.sleep(7)
            assert proc.poll() is None
            proc.terminate()
            assert proc.wait(timeout=8) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        log.seek(0)
        text = log.read()
        assert 'serial read failed' in text
        assert 'retry in' in text
        assert 'Watchdog:' not in text


def test_paho_retries_initial_broker_outage_and_reconnects():
    """Tiny MQTT 3.1.1 peer; exercise the real paho network loop, not mocks."""
    import socket
    import time
    from paho.mqtt.enums import CallbackAPIVersion
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    listener.settimeout(8)
    connected = threading.Event()
    drop = threading.Event()
    finished = threading.Event()
    received = []
    errors = []
    client = paho.Client(CallbackAPIVersion.VERSION2)
    mqtt.register_availability(client, 'taico1')
    def on_connect(client, *args):
        mqtt.mqtt_reconnected(client)
        connected.set()
    client.on_connect = on_connect
    client.reconnect_delay_set(1, 2)
    client.will_set(mqtt.AVAILABILITY_TOPIC, 'offline', qos=1, retain=True)

    def read_n(conn, n):
        data = b''
        while len(data) < n:
            part = conn.recv(n - len(data))
            if not part:
                raise EOFError()
            data += part
        return data
    def packet(conn):
        header = read_n(conn, 1)[0]
        length, shift = 0, 0
        while True:
            byte = read_n(conn, 1)[0]
            length += (byte & 127) << shift
            shift += 7
            if not byte & 128:
                return header, read_n(conn, length)
    def broker():
        try:
            for session in range(2):
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(.2)
                    header, _ = packet(conn)
                    assert header >> 4 == 1  # CONNECT
                    conn.sendall(b'\x20\x02\x00\x00')
                    while not finished.is_set():
                        if session == 0 and drop.is_set():
                            break
                        try:
                            header, body = packet(conn)
                        except socket.timeout:
                            continue
                        except EOFError:
                            break
                        if header >> 4 == 3:
                            n = int.from_bytes(body[:2], 'big')
                            topic = body[2:2+n].decode()
                            qos = (header >> 1) & 3
                            offset = 2+n
                            if qos == 1:
                                conn.sendall(b'\x40\x02' + body[offset:offset+2])
                                offset += 2
                            received.append((session, topic, body[offset:].decode(), bool(header & 1)))
                        elif header >> 4 == 12:
                            conn.sendall(b'\xd0\x00')
                        elif header >> 4 == 14:
                            break
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=broker, daemon=True)
    try:
        client.connect_async('127.0.0.1', port)
        client.loop_start()
        time.sleep(.2)  # not listening: first connect genuinely fails
        assert not connected.is_set()
        listener.listen()
        worker.start()
        assert connected.wait(6)
        connected.clear()
        drop.set()
        assert connected.wait(6)
        deadline = time.monotonic() + 2
        while not any(s == 1 and t == 'taico1/availability' for s, t, _, _ in received):
            assert time.monotonic() < deadline
            time.sleep(.02)
        assert (1, 'taico1/availability', 'offline', True) in received
        assert (1, mqtt.AVAILABILITY_TOPIC, 'online', True) in received
    finally:
        finished.set()
        client.disconnect()
        client.loop_stop()
        listener.close()
        if worker.is_alive():
            worker.join(1)
    assert not errors
