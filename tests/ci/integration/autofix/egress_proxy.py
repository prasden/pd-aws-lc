import asyncio
import sys
from urllib.parse import urlsplit

_PORT = 3128
_ALLOWED = sys.argv[1:]


# Allow a host that equals an entry, or that ends with an entry starting with a dot.
# Example: ".githubusercontent.com" allows "objects.githubusercontent.com".
def _allowed(host: str) -> bool:
    return any(host == entry or (entry.startswith(".") and host.endswith(entry)) for entry in _ALLOWED)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        request_line, rest = head.split(b"\r\n", 1)
        method, target, version = request_line.decode("ascii").split(" ")
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            port, first = int(port), b""
            permitted = port == 443
        else:
            url = urlsplit(target)
            host, port = url.hostname or "", url.port or 80
            path = url.path or "/"
            first = f"{method} {path}{'?' + url.query if url.query else ''} {version}\r\n".encode() + rest
            permitted = url.scheme == "http" and port == 80
        host = host.lower()
        if not (permitted and _allowed(host)):
            print(f"denied {method} {host}:{port}", flush=True)
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
    except (ValueError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as error:
        print(f"failed: {error}", flush=True)
        writer.close()
        return
    if first:
        upstream_writer.write(first)
    else:
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer), return_exceptions=True)


async def _main() -> None:
    server = await asyncio.start_server(_handle, "0.0.0.0", _PORT)
    async with server:
        await server.serve_forever()


asyncio.run(_main())
