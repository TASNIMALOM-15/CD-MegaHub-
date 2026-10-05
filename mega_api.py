"""Minimal MEGA API client: login (v1/v2 + 2FA), listing, shared folders,
download, upload, mkdir, rename, delete, export link."""
import base64
import hashlib
import json
import os
import random
import time

import requests
from Crypto.Cipher import AES

API_URL = "https://g.api.mega.co.nz/cs"
ROOT_NAMES = {2: "Cloud Drive", 3: "Inbox", 4: "Rubbish Bin"}
ERR = {
    -2: "Invalid arguments", -3: "Server busy, try again", -4: "Rate limited",
    -6: "Too many connections", -8: "Expired", -9: "Not found / wrong email or password",
    -11: "Access denied", -12: "Already exists", -15: "Session expired - re-add account",
    -16: "Account blocked", -17: "Storage / transfer quota exceeded",
    -18: "Temporarily unavailable", -26: "2FA code required or wrong",
}


class MegaError(Exception):
    def __init__(self, code, msg=None):
        self.code = code
        super().__init__(msg or ERR.get(code, f"MEGA error {code}"))


class Cancelled(Exception):
    pass


# ---------------- crypto helpers ----------------
def b64d(s):
    s = s.replace(",", "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def pad16(b):
    return b + b"\0" * (-len(b) % 16)


def ecb_enc(key, data):
    return AES.new(key, AES.MODE_ECB).encrypt(data)


def ecb_dec(key, data):
    return AES.new(key, AES.MODE_ECB).decrypt(data)


def enc_attr(attr, key16):
    raw = pad16(b"MEGA" + json.dumps(attr).encode())
    return AES.new(key16, AES.MODE_CBC, iv=bytes(16)).encrypt(raw)


def dec_attr(data, key16):
    data = data[: len(data) // 16 * 16]
    if not data:
        return None
    plain = AES.new(key16, AES.MODE_CBC, iv=bytes(16)).decrypt(data).rstrip(b"\0")
    if not plain.startswith(b"MEGA{"):
        return None
    return json.loads(plain[4:].decode("utf-8", "ignore"))


def mpi_to_int(b):
    return int.from_bytes(b[2:], "big")


def _prepare_key_v1(password):
    pw = password.encode()
    keys = [pad16(pw[i:i + 16]) for i in range(0, len(pw), 16)] or [bytes(16)]
    ciphers = [AES.new(k, AES.MODE_ECB) for k in keys]
    pkey = bytes.fromhex("93C467E37DB0C7A4D1BE3F810152CB56")
    for _ in range(0x10000):
        for c in ciphers:
            pkey = c.encrypt(pkey)
    return pkey


def _stringhash_v1(email, key):
    s = email.encode()
    s += b"\0" * (-len(s) % 4)
    h = bytearray(16)
    for i in range(0, len(s), 4):
        w = (i // 4) % 4
        for j in range(4):
            h[w * 4 + j] ^= s[i + j]
    h = bytes(h)
    c = AES.new(key, AES.MODE_ECB)
    for _ in range(0x4000):
        h = c.encrypt(h)
    return b64e(h[0:4] + h[8:12])


def _chunks(size):
    p, s = 0, 0x20000
    while p + s < size:
        yield p, s
        p += s
        if s < 0x100000:
            s += 0x20000
    if size - p > 0:
        yield p, size - p


class MegaClient:
    def __init__(self):
        self.sid = None
        self.master_key = None
        self.privk_b64 = None
        self.uid = None
        self.email = None
        self._rsa = None
        self.http = requests.Session()
        self.seq = random.randint(0, 0xFFFFFFFF)
        self.nodes = {}
        self._ch = {}
        self.loaded_at = 0

    # ---------- session ----------
    def export_session(self):
        return {"sid": self.sid, "mk": b64e(self.master_key), "privk": self.privk_b64,
                "uid": self.uid, "email": self.email}

    @classmethod
    def from_session(cls, d):
        c = cls()
        c.sid, c.master_key = d["sid"], b64d(d["mk"])
        c.privk_b64, c.uid, c.email = d.get("privk"), d.get("uid"), d.get("email")
        c._load_rsa()
        return c

    # ---------- low level ----------
    def _api(self, cmd, retries=6):
        params = {"id": self.seq}
        self.seq += 1
        if self.sid:
            params["sid"] = self.sid
        delay = 0.5
        for _ in range(retries):
            r = self.http.post(API_URL, params=params, data=json.dumps([cmd]), timeout=60)
            data = r.json()
            if not isinstance(data, int):
                data = data[0]
            if isinstance(data, int) and data < 0:
                if data == -3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise MegaError(data)
            return data
        raise MegaError(-3)

    # ---------- login ----------
    def login(self, email, password, mfa=None):
        email = email.strip().lower()
        pre = self._api({"a": "us0", "user": email})
        if isinstance(pre, dict) and pre.get("s"):
            dk = hashlib.pbkdf2_hmac("sha512", password.encode(), b64d(pre["s"]), 100000, 32)
            pkey, uh = dk[:16], b64e(dk[16:32])
        else:
            pkey = _prepare_key_v1(password)
            uh = _stringhash_v1(email, pkey)
        cmd = {"a": "us", "user": email, "uh": uh}
        if mfa:
            cmd["mfa"] = mfa
        res = self._api(cmd)
        self.master_key = ecb_dec(pkey, b64d(res["k"]))[:16]
        self.privk_b64 = res.get("privk")
        self._load_rsa()
        if "tsid" in res:
            self.sid = res["tsid"]
        elif "csid" in res and self._rsa:
            m = pow(mpi_to_int(b64d(res["csid"])), self._rsa["d"], self._rsa["n"])
            h = "%x" % m
            h = "0" + h if len(h) % 2 else h
            self.sid = b64e(bytes.fromhex(h)[:43])
        else:
            raise MegaError(-9)
        self.email = email
        self.uid = res.get("u")

    def _load_rsa(self):
        if not self.privk_b64 or not self.master_key:
            return
        pk = ecb_dec(self.master_key, pad16(b64d(self.privk_b64)))
        parts = []
        for _ in range(4):
            ln = ((pk[0] * 256 + pk[1] + 7) // 8) + 2
            parts.append(int.from_bytes(pk[2:ln], "big"))
            pk = pk[ln:]
        p, q, d, _u = parts
        self._rsa = {"n": p * q, "d": d}

    def _dec_share_key(self, sk):
        if len(sk) > 22 and self._rsa:  # RSA encrypted (from a contact)
            m = pow(mpi_to_int(b64d(sk)), self._rsa["d"], self._rsa["n"])
            h = "%x" % m
            h = "0" + h if len(h) % 2 else h
            return bytes.fromhex(h)[:16]
        return ecb_dec(self.master_key, b64d(sk))

    # ---------- listing ----------
    def _node_key(self, n, share_keys):
        ks = {}
        for part in n.get("k", "").split("/"):
            if ":" in part:
                a, b = part.split(":", 1)
                ks[a] = b
        if n.get("u") and n.get("u") in ks:
            return ecb_dec(self.master_key, b64d(ks[n["u"]]))
        for sh, enc in ks.items():
            if sh in share_keys:
                return ecb_dec(share_keys[sh], b64d(enc))
        return None

    def load(self):
        res = self._api({"a": "f", "c": 1, "r": 1})
        files = res.get("f", [])
        share_keys = {}
        for o in res.get("ok", []):
            try:
                share_keys[o["h"]] = ecb_dec(self.master_key, b64d(o["k"]))
            except Exception:
                pass
        for n in files:
            if n.get("sk"):
                try:
                    share_keys[n["h"]] = self._dec_share_key(n["sk"])
                except Exception:
                    pass
        nodes = {}
        for n in files:
            t, h = n.get("t"), n["h"]
            node = {"h": h, "p": n.get("p"), "t": t, "u": n.get("u"), "s": n.get("s", 0),
                    "ts": n.get("ts", 0), "su": n.get("su"), "name": None, "key": None, "k16": None}
            if t in ROOT_NAMES:
                node["name"] = ROOT_NAMES[t]
                if t == 2 and n.get("u"):
                    self.uid = n["u"]
            elif t in (0, 1):
                try:
                    key = self._node_key(n, share_keys)
                    if key is None:
                        continue
                    if t == 0:
                        k16 = xor(key[:16], key[16:32])
                    else:
                        key = key[:16]
                        k16 = key
                    attrs = dec_attr(b64d(n["a"]), k16)
                    if attrs is None:
                        continue
                    node.update(name=attrs.get("n", "?"), key=key, k16=k16)
                except Exception:
                    continue
            else:
                continue
            nodes[h] = node
        self.nodes = nodes
        self.loaded_at = time.time()
        self._reindex()

    def _reindex(self):
        self._ch = {}
        for n in self.nodes.values():
            self._ch.setdefault(n["p"], []).append(n)

    def children(self, h):
        return self._ch.get(h, [])

    def root(self, t):
        return next((n["h"] for n in self.nodes.values() if n["t"] == t), None)

    def shared_roots(self):
        return [n for n in self.nodes.values()
                if n["t"] in (0, 1) and n["u"] != self.uid and (n.get("su") or n["p"] not in self.nodes)]

    def walk_files(self, h):
        out, stack = [], [h]
        while stack:
            for n in self.children(stack.pop()):
                if n["t"] == 1:
                    stack.append(n["h"])
                elif n["t"] == 0:
                    out.append(n)
        return out

    def quota(self):
        r = self._api({"a": "uq", "strg": 1})
        return r.get("cstrg", 0), r.get("mstrg", 0)

    # ---------- transfers ----------
    def download(self, node, dest, progress=None, check=None):
        res = self._api({"a": "g", "g": 1, "n": node["h"]})
        if "g" not in res:
            raise MegaError(res.get("e", -5))
        size = res["s"]
        cipher = AES.new(node["k16"], AES.MODE_CTR, nonce=node["key"][16:24])
        done = 0
        with self.http.get(res["g"], stream=True, timeout=60) as r, open(dest, "wb") as f:
            r.raise_for_status()
            for chunk in r.iter_content(1024 * 1024):
                if check:
                    check()
                f.write(cipher.decrypt(chunk))
                done += len(chunk)
                if progress:
                    progress(done, size)

    def _post(self, url, data):
        last = None
        for i in range(5):
            try:
                r = self.http.post(url, data=data, timeout=120)
                if r.status_code == 200:
                    t = r.text.strip()
                    if t.lstrip("-").isdigit() and int(t) < 0:
                        raise MegaError(int(t))
                    return t
                last = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                last = str(e)
            time.sleep(2 * (i + 1))
        raise MegaError(-5, f"Upload failed: {last}")

    def upload(self, path, parent_h, name, progress=None, check=None):
        size = os.path.getsize(path)
        if size == 0:
            raise MegaError(-2, "Empty file")
        url = self._api({"a": "u", "s": size})["p"]
        k, iv = os.urandom(16), os.urandom(8)
        ctr = AES.new(k, AES.MODE_CTR, nonce=iv)
        ecb = AES.new(k, AES.MODE_ECB)
        file_mac, done, handle = bytes(16), 0, ""
        with open(path, "rb") as f:
            for start, length in _chunks(size):
                if check:
                    check()
                plain = f.read(length)
                mac = AES.new(k, AES.MODE_CBC, iv=iv + iv).encrypt(pad16(plain))[-16:]
                file_mac = ecb.encrypt(xor(file_mac, mac))
                handle = self._post(f"{url}/{start}", ctr.encrypt(plain))
                done += length
                if progress:
                    progress(done, size)
        if not handle:
            raise MegaError(-5, "No completion handle")
        meta = xor(file_mac[:4], file_mac[4:8]) + xor(file_mac[8:12], file_mac[12:16])
        nodekey = xor(k, iv + meta) + iv + meta
        res = self._api({"a": "p", "t": parent_h, "n": [{
            "h": handle, "t": 0, "a": b64e(enc_attr({"n": name}, k)),
            "k": b64e(ecb_enc(self.master_key, nodekey))}]})
        return self._register(res["f"][0], nodekey, k, name, size)

    def _register(self, item, key, k16, name, size=0):
        n = {"h": item["h"], "p": item.get("p"), "t": item["t"], "u": self.uid, "s": size,
             "ts": int(time.time()), "su": None, "name": name, "key": key, "k16": k16}
        self.nodes[n["h"]] = n
        self._reindex()
        return n

    # ---------- management ----------
    def mkdir(self, parent_h, name):
        k = os.urandom(16)
        res = self._api({"a": "p", "t": parent_h, "n": [{
            "h": "xxxxxxxx", "t": 1, "a": b64e(enc_attr({"n": name}, k)),
            "k": b64e(ecb_enc(self.master_key, k))}]})
        return self._register(res["f"][0], k, k, name)

    def rename(self, h, new):
        n = self.nodes[h]
        self._api({"a": "a", "n": h, "attr": b64e(enc_attr({"n": new}, n["k16"])),
                   "key": b64e(ecb_enc(self.master_key, n["key"]))})
        n["name"] = new

    def trash(self, h):
        rb = self.root(4)
        self._api({"a": "m", "n": h, "t": rb})
        self.nodes[h]["p"] = rb
        self._reindex()

    def delete_perm(self, h):
        self._api({"a": "d", "n": h})
        stack = [h]
        while stack:
            cur = stack.pop()
            stack += [c["h"] for c in self.children(cur)]
            self.nodes.pop(cur, None)
        self._reindex()

    def export_link(self, node):
        if node["t"] != 0:
            raise MegaError(-2, "Folder links are not supported yet")
        ph = self._api({"a": "l", "n": node["h"]})
        return f"https://mega.nz/file/{ph}#{b64e(node['key'])}"
