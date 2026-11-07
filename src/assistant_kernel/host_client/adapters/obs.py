"""OBS adapter: obs-websocket v5, read-only requests only (GetVersion, GetRecordStatus, GetSceneList)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
from pathlib import Path

ACTIONS = {"obs.status": ("GetVersion", "GetRecordStatus"), "obs.scenes": ("GetSceneList",)}


class _WS:
    def __init__(self, host: str, port: int, timeout: float = 10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                           "Sec-WebSocket-Protocol: obswebsocket.json\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("OBS closed the connection")
            head += chunk
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError("OBS websocket handshake refused")
        self.buffer = head.split(b"\r\n\r\n", 1)[1]

    def _read(self, n: int) -> bytes:
        while len(self.buffer) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("OBS closed the connection")
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def _send_frame(self, data: bytes, opcode: int = 1) -> None:
        mask = os.urandom(4)
        n = len(data)
        head = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send(self, obj: dict) -> None:
        self._send_frame(json.dumps(obj).encode())

    def recv(self) -> dict:
        message = bytearray()
        while True:
            first, second = self._read(2)
            opcode, n = first & 15, second & 127
            if second & 128:
                raise ConnectionError('Unexpected masked server frame')
            if n == 126:
                n = struct.unpack('>H', self._read(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', self._read(8))[0]
            if n + len(message) > 1024 * 1024:
                raise ConnectionError('OBS response exceeds limit')
            data = self._read(n)
            if opcode == 8:
                raise ConnectionError('OBS closed the websocket')
            if opcode == 9:
                self._send_frame(data, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise ConnectionError('Unexpected OBS frame type')
            message.extend(data)
            if first & 128:
                return json.loads(message)

    def close(self) -> None:
        self.sock.close()


class ObsAdapter:
    def __init__(self, cfg: dict, base: Path):
        self.host, self.port = cfg.get("host", "127.0.0.1"), int(cfg.get("port", 4455))
        file = cfg.get("password_file")
        self.password = (base / file).read_text("utf-8").strip() if file else ""

    def _auth(self, auth: dict) -> str:
        secret = base64.b64encode(hashlib.sha256((self.password + auth["salt"]).encode()).digest()).decode()
        return base64.b64encode(hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()

    def run(self, action: str) -> dict:
        if action not in ACTIONS:
            raise ValueError("action is not supported")
        ws = _WS(self.host, self.port)
        try:
            hello = ws.recv()["d"]
            ident = {"rpcVersion": 1, "eventSubscriptions": 0}
            if "authentication" in hello:
                ident["authentication"] = self._auth(hello["authentication"])
            ws.send({"op": 1, "d": ident})
            if ws.recv().get("op") != 2:
                raise PermissionError("OBS authentication failed")
            out = {}
            for i, request in enumerate(ACTIONS[action]):
                ws.send({"op": 6, "d": {"requestType": request, "requestId": f"r{i}"}})
                reply = ws.recv()["d"]
                if not reply["requestStatus"]["result"]:
                    raise RuntimeError(f"{request} failed")
                out[request] = reply.get("responseData", {})
            if action == "obs.scenes":
                data = out["GetSceneList"]
                return {"scenes": [s["sceneName"] for s in data.get("scenes", [])],
                        "current": data.get("currentProgramSceneName")}
            ver, rec = out["GetVersion"], out["GetRecordStatus"]
            return {"obs_version": ver.get("obsVersion"), "websocket_version": ver.get("obsWebSocketVersion"),
                    "recording": bool(rec.get("outputActive"))}
        finally:
            ws.close()
