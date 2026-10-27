from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import queue
import subprocess
import sys
import threading

import click
import httpx

from .. import jncweb, utils
from ..config import ENVVAR_PREFIX

logger = logging.getLogger(__name__)
console = utils.getConsole()

ALLOWED_COMMANDS = ("epub", "update")


@click.command(
    name="webhook",
    help=(
        "Run an HTTP server that generates EPUB files when it receives a POST "
        "(for example from an RSS automation like Activepieces). JSON body: "
        '{"url": "<JNC URL>", "command": "epub|update"}'
    ),
)
@click.option(
    "--host",
    default="0.0.0.0",
    show_default=True,
    envvar=f"{ENVVAR_PREFIX}WEBHOOK_HOST",
    help="Interface to listen on",
)
@click.option(
    "--port",
    default=8765,
    show_default=True,
    type=int,
    envvar=f"{ENVVAR_PREFIX}WEBHOOK_PORT",
    help="Port to listen on",
)
@click.option(
    "--token",
    envvar=f"{ENVVAR_PREFIX}WEBHOOK_TOKEN",
    help=(
        "Shared secret: requests must send it in the 'X-Webhook-Token' header "
        "or as '?token=' in the URL"
    ),
)
@click.option(
    "--default-command",
    type=click.Choice(ALLOWED_COMMANDS),
    default="epub",
    show_default=True,
    envvar=f"{ENVVAR_PREFIX}WEBHOOK_COMMAND",
    help="Command to run when the request body does not specify one",
)
@click.option(
    "--env-file",
    default=".env",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="File with KEY=VALUE lines (e.g. JNCEP_EMAIL) loaded before starting",
)
@click.option(
    "--kavita-url",
    envvar=f"{ENVVAR_PREFIX}KAVITA_URL",
    help="Base URL of a Kavita server to rescan after each run (e.g. "
    "http://localhost:5000)",
)
@click.option(
    "--kavita-api-key",
    envvar=f"{ENVVAR_PREFIX}KAVITA_API_KEY",
    help="API key of a Kavita admin user (User settings > 3rd Party Clients)",
)
@click.option(
    "--kavita-folder",
    envvar=f"{ENVVAR_PREFIX}KAVITA_FOLDER",
    help=(
        "Output folder as seen by Kavita (e.g. /books/jnc inside Docker); "
        "must be inside one of the Kavita library folders"
    ),
)
@click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
def run_webhook(
    host,
    port,
    token,
    default_command,
    env_file,
    kavita_url,
    kavita_api_key,
    kavita_folder,
    extra_args,
):
    # load first so the Kavita options can also come from the env file
    _load_env_file(env_file)
    kavita_url = kavita_url or os.environ.get(f"{ENVVAR_PREFIX}KAVITA_URL")
    kavita_api_key = kavita_api_key or os.environ.get(
        f"{ENVVAR_PREFIX}KAVITA_API_KEY"
    )
    kavita_folder = kavita_folder or os.environ.get(f"{ENVVAR_PREFIX}KAVITA_FOLDER")

    kavita = None
    if kavita_url:
        if not kavita_folder:
            raise click.UsageError("--kavita-url requires --kavita-folder")
        if not kavita_api_key:
            # expected on first start: the API key only exists once the Kavita
            # admin user has been created
            console.warning("No --kavita-api-key set: Kavita will not be rescanned")
        else:
            kavita = (kavita_url.rstrip("/"), kavita_api_key, kavita_folder)
            console.info(f"Kavita rescan of '{kavita_folder}' enabled after each run")

    if not token:
        console.warning(
            "No --token set: anyone who can reach this server can trigger downloads!"
        )

    jobs = queue.Queue()
    # single worker so runs never overlap (they share the tracking file + output)
    worker = threading.Thread(
        target=_worker, args=(jobs, list(extra_args), kavita), daemon=True
    )
    worker.start()

    handler = _make_handler(jobs, token, default_command)
    server = ThreadingHTTPServer((host, port), handler)
    console.info(f"Listening on http://{host}:{port}/ (POST to trigger)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _make_handler(jobs, token, default_command):
    class WebhookHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            # health check
            self._reply(200, {"status": "ok", "queued": jobs.qsize()})

        def do_POST(self):
            if token and not self._has_valid_token():
                self._reply(401, {"error": "invalid token"})
                return

            try:
                payload = self._read_json()
            except ValueError as ex:
                self._reply(400, {"error": f"invalid JSON: {ex}"})
                return

            command = payload.get("command") or default_command
            url = _find_url(payload)

            if command not in ALLOWED_COMMANDS:
                self._reply(400, {"error": f"command must be one of {ALLOWED_COMMANDS}"})
                return
            if command == "epub" and not url:
                self._reply(400, {"error": "'url' is required for epub"})
                return
            if url:
                try:
                    jncweb.resource_from_url(url)
                except Exception as ex:
                    self._reply(400, {"error": f"not a J-Novel Club URL: {ex}"})
                    return

            jobs.put((command, url))
            self._reply(202, {"status": "queued", "command": command, "url": url})

        def _has_valid_token(self):
            if self.headers.get("X-Webhook-Token") == token:
                return True
            query = self.path.partition("?")[2]
            return f"token={token}" in query.split("&")

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data

        def _reply(self, status, body):
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            logger.info("%s - %s", self.address_string(), format % args)

    return WebhookHandler


def _find_url(payload):
    # accept a few common key names so the raw RSS item from Activepieces can be
    # forwarded as is
    for key in ("url", "link", "jnc_url"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _worker(jobs, extra_args, kavita):
    while True:
        command, url = jobs.get()
        args = [sys.executable, "-m", "jncep.jncep", command, *extra_args]
        if url:
            args.append(url)
        # do not log the full args: extra_args may contain credentials
        console.info(f"Run '{command}' for {url or 'all tracked series'}...")
        try:
            # subprocess: jncep commands run their own trio loop and may exit
            # the process on error
            result = subprocess.run(args, env=os.environ.copy())
            if result.returncode == 0:
                console.info(f"Done '{command}' for {url or 'all tracked series'}")
                if kavita:
                    _scan_kavita(*kavita)
            else:
                console.error(
                    f"'{command}' for {url or 'all tracked series'} failed "
                    f"(exit code {result.returncode})"
                )
        except Exception as ex:
            console.error(f"Could not run '{command}': {ex}")
        finally:
            jobs.task_done()


def _scan_kavita(base_url, api_key, folder):
    # Kavita folder watching is unreliable on Docker bind mounts (esp. Windows)
    # so ask for a scan explicitly; Kavita queues it and returns immediately
    try:
        response = httpx.post(
            f"{base_url}/api/Library/scan-folder",
            json={"apiKey": api_key, "folderPath": folder},
            timeout=30,
        )
        response.raise_for_status()
        console.info(f"Kavita scan of '{folder}' requested")
    except Exception as ex:
        console.error(f"Kavita scan request failed: {ex}")


def _load_env_file(path):
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # real environment wins over the file
            os.environ.setdefault(key, value)
