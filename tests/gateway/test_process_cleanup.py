"""Owned worker cleanup, independent gateways, and safe process identities."""

import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import threading

import pytest

from nanobot.gateway import GatewayRuntime, GatewayRuntimePaths
from nanobot.gateway.process_tree import owned_descendants, process_snapshot


def snapshot(entries, identity="snapshot-start"):
    return {pid: (*entry, identity) for pid, entry in entries.items()}


def test_descendants_exclude_gateway_and_controller_branches():
    processes = snapshot({
        10: (1, ["nanobot", "gateway"]),
        11: (10, ["worker"]), 12: (11, ["worker-child"]),
        20: (10, ["python", "-m", "nanobot", "gateway"]), 21: (20, ["peer-worker"]),
        30: (10, ["restart-helper"]), 31: (30, ["nanobot", "restart"]),
        40: (10, ["custom-gateway-entry"]), 41: (40, ["peer-worker"]),
        50: (1, ["same-group-but-unrelated"]),
    })
    assert set(owned_descendants(processes, 10, 31, {40})) == {11, 12}


def test_posix_snapshot_ignores_zombies_and_reports_probe_failure():
    def probe(*args, **kwargs):
        assert kwargs["timeout"] == 2
        return subprocess.CompletedProcess(args, 0, stdout=(
            '11 10 S Mon Nov 16 10:00:00 2026 /some/python -m worker\n'
            '12 10 Z Mon Nov 16 10:00:00 2026 <defunct>\n'
            '20 10 S Mon Nov 16 10:00:00 2026 "/path with spaces/nanobot" gateway --foreground\n'
            '21 10 S Mon Nov 16 10:00:00 2026 sh -c echo "unterminated\n'
        ))
    assert process_snapshot("Darwin", probe) == snapshot({
        11: (10, ["/some/python", "-m", "worker"]),
        20: (10, ["/path with spaces/nanobot", "gateway", "--foreground"]),
        21: (10, ["sh", "-c", "echo", '"unterminated']),
    }, identity="posix:Mon Nov 16 10:00:00 2026")
    def unavailable(*a, **kw):
        raise FileNotFoundError
    assert process_snapshot("Darwin", unavailable) is None


def test_cleanup_never_signals_reused_or_unverifiable_worker(tmp_path, monkeypatch):
    runtime = GatewayRuntime(paths=GatewayRuntimePaths.for_instance(data_dir=tmp_path))
    monkeypatch.setattr(runtime, "_posix_snapshot", lambda: snapshot({11: (1, ["worker"])}))
    monkeypatch.setattr(runtime, "_process_identity", lambda _: "new-process")
    monkeypatch.setattr("nanobot.gateway.runtime.os.kill", lambda *a: pytest.fail("signal sent"))
    assert runtime._stop_posix_children(10, {11: "old-process"})
    monkeypatch.setattr(runtime, "_process_identity", lambda _: None)
    monkeypatch.setattr(runtime, "_is_pid_running", lambda _: True)
    assert not runtime._stop_posix_children(10, {11: "old-process"})


def test_snapshot_preserves_start_time_spacing_for_identity_checks():
    def probe(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0,
            stdout="31 10 S Mon Nov  3 10:00:00 2026 worker\n")
    assert process_snapshot("Darwin", probe)[31][2] == "posix:Mon Nov  3 10:00:00 2026"


def test_cleanup_preserves_worker_that_became_a_gateway(tmp_path, monkeypatch):
    runtime = GatewayRuntime(paths=GatewayRuntimePaths.for_instance(data_dir=tmp_path))
    monkeypatch.setattr(runtime, "_posix_snapshot", lambda: snapshot({
        11: (1, ["python", "-m", "nanobot", "gateway"]),
        12: (11, ["peer-worker"]),
    }))
    monkeypatch.setattr(runtime, "_process_identity", lambda _: "same-start")
    monkeypatch.setattr("nanobot.gateway.runtime.os.kill", lambda *a: pytest.fail("signal sent"))
    assert runtime._stop_posix_children(10, {11: "same-start", 12: "same-start"})


def test_unavailable_ownership_does_not_stop_gateway(tmp_path, monkeypatch):
    runtime = GatewayRuntime(paths=GatewayRuntimePaths.for_instance(data_dir=tmp_path))
    monkeypatch.setattr(runtime, "_posix_snapshot", lambda: None)
    monkeypatch.setattr("nanobot.gateway.runtime.os.kill", lambda *a: pytest.fail("signal sent"))
    assert not runtime._terminate_posix(10, timeout_s=0)


def test_parent_pid_reuse_does_not_capture_or_kill_the_replacement(tmp_path, monkeypatch):
    runtime = GatewayRuntime(paths=GatewayRuntimePaths.for_instance(data_dir=tmp_path))
    processes = {10: (1, ["gateway"], "original"), 11: (10, ["worker"], "worker-start")}
    monkeypatch.setattr(runtime, "_posix_snapshot", lambda: processes)
    parent_versions = iter(["original", "original", "replacement"])
    monkeypatch.setattr(runtime, "_process_identity",
                        lambda pid: next(parent_versions) if pid == 10 else "worker-start")
    monkeypatch.setattr(runtime, "_wait_for_exit", lambda *_: False)
    signals = []
    monkeypatch.setattr("nanobot.gateway.runtime.os.kill", lambda pid, sig: signals.append((pid, sig)))
    cleaned = []
    monkeypatch.setattr(runtime, "_stop_posix_children",
                        lambda root, owned: cleaned.append(owned.copy()) or True)
    assert runtime._terminate_posix(10, timeout_s=0)
    assert signals == [(10, signal.SIGTERM)]
    assert cleaned == [{11: "worker-start"}]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process ownership")
@pytest.mark.parametrize("forced", [False, True])
def test_real_stop_cleans_owned_listener_and_preserves_peer_gateway(tmp_path, forced):
    listener = (
        "import json,os,signal,socket,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "s=socket.socket();s.bind(('127.0.0.1',0));s.listen(1);"
        "print(json.dumps({'pid':os.getpid(),'port':s.getsockname()[1]}),flush=True);"
        "time.sleep(30)"
    )
    parent = (
        "import json,signal,subprocess,sys,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN if sys.argv[2]=='1' else signal.SIG_DFL);"
        "w=subprocess.Popen([sys.executable,'-c',sys.argv[1]],stdout=subprocess.PIPE,text=True,start_new_session=True);"
        "p=subprocess.Popen([sys.executable,'-c',sys.argv[1]],stdout=subprocess.PIPE,text=True);"
        "print(json.dumps([json.loads(w.stdout.readline()),json.loads(p.stdout.readline())]),flush=True);"
        "time.sleep(30)"
    )
    process = subprocess.Popen([sys.executable, "-c", parent, listener, str(int(forced))],
                               stdout=subprocess.PIPE, text=True, start_new_session=True)
    workers = []
    reaper = threading.Thread(target=process.wait, daemon=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5), "isolated worker startup timed out"
        workers = json.loads(process.stdout.readline())
        reaper.start()
        runtime = GatewayRuntime(paths=GatewayRuntimePaths.for_instance(data_dir=tmp_path))
        runtime.paths.run_dir.mkdir(parents=True)
        peer = workers[1]
        (runtime.paths.run_dir / "gateway.peer.json").write_text(json.dumps({
            "pid": peer["pid"], "identity": runtime._process_identity(peer["pid"]),
        }))
        assert runtime._terminate_posix(process.pid, timeout_s=0 if forced else 2)
        assert process.wait(timeout=3) is not None
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", workers[0]["port"])) != 0
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", peer["port"])) == 0
    finally:
        # These processes were created solely by this test in its own session.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        for worker in workers:
            try:
                os.kill(worker["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        if not workers:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.stdout.close()
