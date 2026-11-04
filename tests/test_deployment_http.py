import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.error import HTTPError

import pytest

from mlops_cp.deployments import DeploymentCommand, HTTPDeploymentTarget


@contextmanager
def controller(*, redirect_to=None, redirect_code=302):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append((self.command, self.headers.get("Authorization"), body))
            if redirect_to is not None:
                self.send_response(redirect_code)
                self.send_header("Location", redirect_to)
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"accepted":true,"deployment_id":"local-test"}')

        def do_GET(self):
            received.append((self.command, self.headers.get("Authorization"), b""))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"accepted":true}')

        def log_message(self, *_args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/deploy", received
        finally:
            server.shutdown()
            thread.join(timeout=5)


def deployment_command():
    return DeploymentCommand("demo:1", "file:///synthetic-model", "set_traffic", 5, "test")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_never_forwards_credentials_or_commands(status):
    # Both controllers are disposable loopback fixtures. A different port is a
    # different origin; the synthetic bearer must stay at the configured target.
    with controller() as (other_origin, leaked), controller(
        redirect_to=other_origin, redirect_code=status
    ) as (endpoint, received):
        target = HTTPDeploymentTarget(endpoint, bearer_token="synthetic-test-token")
        with pytest.raises(HTTPError) as error:
            target.execute(deployment_command())

        assert error.value.code == status
        assert len(received) == 1
        assert leaked == []


def test_direct_controller_receives_authenticated_post():
    with controller() as (endpoint, received):
        target = HTTPDeploymentTarget(endpoint, bearer_token="synthetic-test-token")
        receipt = target.execute(deployment_command())

    assert receipt.accepted is True
    assert receipt.deployment_id == "local-test"
    method, authorization, payload = received[0]
    assert method == "POST"
    assert authorization == "Bearer synthetic-test-token"
    assert json.loads(payload)["traffic_percent"] == 5
