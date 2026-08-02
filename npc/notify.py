"""Telegram: the loop's only output to a human.

urllib rather than requests, because nothing here justifies a dependency. The
whole system exists to deliver these messages, so the transport retries a
couple of times, falls back from photo to text, and never raises into the loop
- a failed send must not take the watcher down with it.
"""

import json
import time
import urllib.error
import urllib.request
import uuid

API = "https://api.telegram.org/bot{token}/{method}"
ATTEMPTS = 3
TIMEOUT = 20
BACKOFF = 3.0


class Disabled:
    """--no-telegram: the loop still runs and still logs, it just says nothing."""

    configured = False

    def alert(self, text, png=None, filename="screen.png"):
        return False


class Telegram:
    configured = True

    def __init__(self, token, chat_id, log=None):
        # The token is a credential: it is never logged and never reaches a
        # message body. Same rule as the RustDesk password.
        self._token = token
        self._chat_id = chat_id
        self._log = log or (lambda message: None)

    def alert(self, text, png=None, filename="screen.png"):
        """Send the message, with the screenshot when there is one."""
        if png is not None:
            if self._post("sendPhoto", {"caption": text}, ("photo", filename, png)):
                return True
            # The message matters more than the picture: a caption too long, a
            # file too big or a slow upload must not swallow the alert.
            self._log("telegram: photo failed, falling back to text")
        return self._post("sendMessage", {"text": text})

    def _post(self, method, fields, file=None):
        fields = dict(fields, chat_id=self._chat_id)
        if file is None:
            body = json.dumps(fields).encode()
            content_type = "application/json"
        else:
            content_type, body = _multipart(fields, file)

        url = API.format(token=self._token, method=method)
        for attempt in range(1, ATTEMPTS + 1):
            request = urllib.request.Request(
                url, data=body, headers={"Content-Type": content_type}
            )
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                    if json.loads(response.read()).get("ok"):
                        return True
                    detail = "telegram replied ok=false"
            except urllib.error.HTTPError as exc:
                detail = f"HTTP {exc.code}: {exc.read()[:200].decode('utf-8', 'replace')}"
            except Exception as exc:  # network down, DNS, timeout, bad JSON
                detail = f"{type(exc).__name__}: {exc}"

            self._log(f"telegram {method} attempt {attempt}/{ATTEMPTS} failed: {self._scrub(detail)}")
            if attempt < ATTEMPTS:
                time.sleep(BACKOFF * attempt)
        return False

    def _scrub(self, text):
        return text.replace(self._token, "<token>") if self._token else text


def _multipart(fields, file):
    name, filename, data = file
    boundary = uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode()
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", bytes(body)


def from_env(credentials, log=None, enabled=True):
    """A Telegram sender, or Disabled when it is switched off or unconfigured."""
    token, chat_id = credentials
    if not enabled or not token or not chat_id:
        return Disabled()
    return Telegram(token, chat_id, log)
