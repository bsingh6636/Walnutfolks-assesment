import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts import simulate


def test_simulator_reuses_http_socket_and_reports_server_time(monkeypatch, capsys):
    peer_ports = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            peer_ports.append(self.client_address[1])
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(404 if self.path == "/missing" else 200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Server-Timing", "app;dur=12.34")
            self.send_header("X-Request-ID", "test-request")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    monkeypatch.setattr(simulate, "HTTP_CLIENT", threading.local())
    monkeypatch.setattr(simulate, "TRACE_HTTP", True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        assert simulate.http_request(url)[0:2] == (200, {"status": "ok"})
        assert simulate.http_request(url + "/missing")[0:2] == (404, {"status": "ok"})
        assert len(peer_ports) == 2
        assert peer_ports[0] == peer_ports[1]
        trace = capsys.readouterr().out
        assert "server_timing=app;dur=12.34" in trace
        assert "connection=new" in trace
        assert "connection=reused" in trace
        assert "request_id=test-request" in trace
    finally:
        conn = getattr(simulate.HTTP_CLIENT, "connection", None)
        if conn is not None:
            conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
