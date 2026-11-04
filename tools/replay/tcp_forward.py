"""Forward localhost ports to the isolated replay worker (live view only).

The worker stays on the internal network without Internet; this small relay
sits on both networks and forwards only the live-view ports.
"""

from __future__ import annotations

import argparse
import socket
import threading


def pipe(source: socket.socket, target: socket.socket) -> None:
    try:
        while data := source.recv(65536):
            target.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (source, target):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


def serve(listen_port: int, host: str, port: int) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", listen_port))
    server.listen(16)
    while True:
        client, _ = server.accept()
        try:
            upstream = socket.create_connection((host, port), timeout=5)
            upstream.settimeout(None)
        except OSError:
            client.close()
            continue
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="replay-worker")
    parser.add_argument("--first-port", type=int, default=8090)
    parser.add_argument("--count", type=int, default=6)
    args = parser.parse_args()
    for offset in range(args.count):
        port = args.first_port + offset
        threading.Thread(target=serve, args=(port, args.host, port), daemon=True).start()
    print(f"Forwarding ports {args.first_port}-{args.first_port + args.count - 1} to {args.host}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
