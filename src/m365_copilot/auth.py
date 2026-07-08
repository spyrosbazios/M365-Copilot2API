import json, urllib.request, urllib.parse, uuid, time, os, ssl, hashlib, base64
import subprocess, tempfile, shutil
import socket as _wsock, struct as _wstruct

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
AUTHORIZE_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"


class TokenRefreshError(Exception):
    pass


class TokenManager:
    def __init__(self, tenant, client_id, scope, rt_file, cache_file):
        self.tenant = tenant
        self.client_id = client_id
        self.scope = scope
        self.rt_file = rt_file
        self.cache_file = cache_file
        self._token_url = TOKEN_URL.format(tenant=tenant)

    def _read_rt(self):
        with open(self.rt_file) as f:
            raw = f.read().strip()
        if not raw:
            raise TokenRefreshError("Refresh token file is empty")
        from .scripts.crypto import decrypt
        try:
            return decrypt(raw)
        except Exception as e:
            raise TokenRefreshError(
                f"无法解密 refresh token: {e}\n"
                f"可能原因: 加密密钥文件 (~/.m365-copilot/encryption.key) 不匹配或已损坏\n"
                f"解决方法: 重新运行 m365-copilot-setup"
            )

    def _write_rt(self, token):
        from .scripts.crypto import encrypt
        with open(self.rt_file, 'w') as f:
            f.write(encrypt(token))

    def refresh(self):
        if not os.path.exists(self.rt_file):
            raise TokenRefreshError(f"Refresh token not found: {self.rt_file}")
        rt = self._read_rt()
        data = urllib.parse.urlencode({
            'client_id': self.client_id, 'refresh_token': rt,
            'grant_type': 'refresh_token', 'scope': self.scope,
        }).encode()
        req = urllib.request.Request(self._token_url, data=data)
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        req.add_header('User-Agent', 'Mozilla/5.0')
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context()) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = json.loads(e.read())
            desc = err.get('error_description', '')[:300]
            code = err.get('error', '')
            if 'AADSTS700082' in desc or 'AADSTS700022' in desc:
                hint = "Refresh token 已过期。请重新运行 m365-copilot-setup"
            else:
                hint = f"请重新运行 m365-copilot-setup"
            raise TokenRefreshError(
                f"Refresh failed: {code}: {desc}\n{hint}"
            )
        if 'refresh_token' in result:
            self._write_rt(result['refresh_token'])
        cache = {
            'access_token': result['access_token'],
            'expires_at': time.time() + result.get('expires_in', 3600),
        }
        with open(self.cache_file, 'w') as f:
            json.dump(cache, f)
        return result['access_token']

    def get(self):
        try:
            with open(self.cache_file) as f:
                cache = json.load(f)
            if cache['expires_at'] > time.time() + 60:
                return cache['access_token']
        except Exception:
            pass
        return self.refresh()

    def setup(self):
        result = self._try_auth_code()
        if isinstance(result, dict):
            return result
        return False

    def _try_auth_code(self):
        verifier = base64.urlsafe_b64encode(os.urandom(64)).rstrip(b'=').decode()
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b'=').decode()
        redirect_uri = 'https://login.microsoftonline.com/common/oauth2/nativeclient'
        params = {
            'client_id': self.client_id, 'response_type': 'code',
            'response_mode': 'query',
            'scope': self.scope,
            'redirect_uri': redirect_uri,
            'state': uuid.uuid4().hex, 'nonce': uuid.uuid4().hex,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
            'prompt': 'select_account',
        }
        auth_url = AUTHORIZE_URL.format(tenant=self.tenant) + "?" + urllib.parse.urlencode(params)

        code = self._capture_code_cdp(auth_url)
        if not code:
            code = self._capture_code_manual(auth_url)
        if not code:
            return False

        data = urllib.parse.urlencode({
            'client_id': self.client_id, 'code': code,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code', 'code_verifier': verifier,
        }).encode()
        req = urllib.request.Request(self._token_url, data=data)
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context()) as resp:
                token_result = json.loads(resp.read())
            if 'refresh_token' in token_result:
                self._save_tokens(token_result)
                return token_result
        except urllib.error.HTTPError as e:
            err = json.loads(e.read())
            print(f"\nToken 交换失败: {err.get('error')}: {err.get('error_description', '')[:200]}")
        return False

    def _find_browser(self):
        candidates = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            shutil.which("msedge") or "",
            shutil.which("chrome") or "",
            shutil.which("google-chrome") or "",
            shutil.which("chromium") or "",
        ]
        for p in candidates:
            if p and os.path.exists(p):
                return p
        return ""

    def _cdp_ws_connect(self, ws_url):
        parsed = urllib.parse.urlparse(ws_url)
        port = parsed.port or 9222
        s = _wsock.create_connection(("127.0.0.1", port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall(
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                s.close()
                return None
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n")[0]:
            s.close()
            return None
        return s

    def _cdp_ws_send(self, sock, data):
        payload = json.dumps(data).encode()
        frame = bytearray([0x81])
        n = len(payload)
        if n < 126:
            frame.append(n)
        elif n < 65536:
            frame.extend([126, *n.to_bytes(2, 'big')])
        else:
            frame.extend([127, *n.to_bytes(8, 'big')])
        mask = os.urandom(4)
        frame.extend(mask)
        frame.extend(payload[i] ^ mask[i % 4] for i in range(n))
        sock.sendall(bytes(frame))

    def _cdp_ws_recv(self, sock):
        def recvn(n):
            d = b""
            while len(d) < n:
                c = sock.recv(n - len(d))
                if not c:
                    return None
                d += c
            return d

        b2 = recvn(2)
        if not b2 or len(b2) < 2:
            return None
        op = b2[0] & 0x0F
        masked = bool(b2[1] & 0x80)
        n = b2[1] & 0x7F
        if n == 126:
            b = recvn(2)
            if not b: return None
            n = int.from_bytes(b, 'big')
        elif n == 127:
            b = recvn(8)
            if not b: return None
            n = int.from_bytes(b, 'big')
        m = recvn(4) if masked else b""
        if not m and masked:
            return None
        p = recvn(n)
        if not p:
            return None
        if masked:
            p = bytes(p[i] ^ m[i % 4] for i in range(n))
        if op == 0x8:
            return None
        if op == 0x9:
            sock.sendall(bytes([0x8A, 0x00]))
            return None
        return json.loads(p) if p else None

    def _find_free_port(self):
        s = _wsock.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _capture_code_cdp(self, auth_url):
        browser = self._find_browser()
        if not browser:
            return None
        tmpdir = tempfile.mkdtemp()
        port = self._find_free_port()
        proc = None
        try:
            proc = subprocess.Popen([
                browser, f"--remote-debugging-port={port}",
                f"--user-data-dir={tmpdir}",
                "--no-first-run", "--no-default-browser-check",
                auth_url,
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            resp = None
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    resp = urllib.request.urlopen(
                        f"http://localhost:{port}/json", timeout=3)
                    break
                except Exception:
                    time.sleep(0.5)

            if not resp:
                return None

            tabs = json.loads(resp.read())
            if not tabs:
                return None
            ws_url = tabs[0].get("webSocketDebuggerUrl")
            if not ws_url:
                return None

            sock = self._cdp_ws_connect(ws_url)
            if not sock:
                return None

            self._cdp_ws_send(sock, {"id": 1, "method": "Page.enable"})
            # Enable Network too for immediate redirect capture
            self._cdp_ws_send(sock, {"id": 2, "method": "Network.enable"})

            print("\n浏览器已打开，请在浏览器中登录...")
            deadline = time.time() + 300
            while time.time() < deadline:
                sock.settimeout(0.2)
                try:
                    msg = self._cdp_ws_recv(sock)
                    if msg and msg.get("method") in ("Page.frameNavigated", "Network.requestWillBeSent"):
                        url = ""
                        if msg["method"] == "Page.frameNavigated":
                            url = msg["params"]["frame"].get("url", "")
                        elif msg["method"] == "Network.requestWillBeSent":
                            url = msg["params"]["request"].get("url", "")
                        parsed = urllib.parse.urlparse(url)
                        codes = urllib.parse.parse_qs(parsed.query).get("code")
                        if codes:
                            try:
                                for t in json.loads(urllib.request.urlopen(
                                        f"http://localhost:{port}/json", timeout=3).read()):
                                    if "?code=" in t.get("url", ""):
                                        tid = t.get("id")
                                        if tid:
                                            urllib.request.urlopen(
                                                f"http://localhost:{port}/json/close/{tid}",
                                                timeout=3)
                            except Exception:
                                pass
                            return codes[0]
                except _wsock.timeout:
                    # Send ping to keep alive
                    try:
                        sock.sendall(bytes([0x89, 0x00]))
                    except Exception:
                        pass
                    continue
                except Exception:
                    pass
        finally:
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            shutil.rmtree(tmpdir, ignore_errors=True)
        return None

    def _capture_code_manual(self, auth_url):
        print("\n" + "=" * 60)
        print("打开下方链接并登录")
        print("登录后浏览器会跳转到一个警告页")
        print("复制地址栏的完整 URL 并粘贴到下面")
        print("\n" + auth_url)
        print("=" * 60)
        url_input = input("\n粘贴跳转后的完整 URL: ").strip()
        if not url_input:
            return None
        parsed = urllib.parse.urlparse(url_input)
        code = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
        if not code:
            code = urllib.parse.parse_qs(parsed.fragment).get("code", [None])[0]
        if not code:
            print("URL 中未找到 authorization code")
            return None
        return code

    def _save_tokens(self, result):
        if 'refresh_token' in result:
            from .scripts.crypto import encrypt
            with open(self.rt_file, 'w') as f:
                f.write(encrypt(result['refresh_token']))
        cache = {
            'access_token': result['access_token'],
            'expires_at': time.time() + result.get('expires_in', 3600),
        }
        with open(self.cache_file, 'w') as f:
            json.dump(cache, f)
        print("认证成功！Refresh token 已保存")
