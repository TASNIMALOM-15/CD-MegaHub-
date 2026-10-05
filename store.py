"""Persistent storage for MEGA sessions. Sessions are encrypted with SECRET_KEY.
Uses MongoDB if MONGO_URI is set (recommended on Render), else a local JSON file."""
import base64
import hashlib
import json
import os
import threading

from cryptography.fernet import Fernet


class Store:
    def __init__(self):
        secret = os.environ.get("SECRET_KEY")
        if not secret:
            raise SystemExit("SECRET_KEY environment variable is required")
        self.fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
        uri = os.getenv("MONGO_URI")
        self.lock = threading.Lock()
        if uri:
            from pymongo import MongoClient
            self.col = MongoClient(uri, serverSelectionTimeoutMS=10000)[os.getenv("MONGO_DB", "megabot")]["accounts"]
            self.col.find_one()
        else:
            self.col = None
            self.path = os.getenv("DATA_FILE", "accounts.json")
            print("WARNING: MONGO_URI not set - accounts are stored in a local file and will be lost on Render restarts")

    def _enc(self, d):
        return self.fernet.encrypt(json.dumps(d).encode()).decode()

    def _dec(self, s):
        return json.loads(self.fernet.decrypt(s.encode()))

    def _read(self):
        if not os.path.exists(self.path):
            return {}
        with open(self.path) as f:
            return json.load(f)

    def _write(self, data):
        with open(self.path, "w") as f:
            json.dump(data, f)

    def all(self):
        if self.col is not None:
            docs = list(self.col.find())
        else:
            with self.lock:
                docs = [dict(v, _id=k) for k, v in self._read().items()]
        out = []
        for d in docs:
            try:
                out.append({"aid": d["_id"], "owner": d["owner"], "email": d["email"],
                            "session": self._dec(d["session"])})
            except Exception:
                print("Could not decrypt account", d.get("_id"), "- wrong SECRET_KEY?")
        return out

    def add(self, aid, owner, email, session):
        doc = {"owner": owner, "email": email, "session": self._enc(session)}
        if self.col is not None:
            self.col.replace_one({"_id": aid}, dict(doc, _id=aid), upsert=True)
        else:
            with self.lock:
                data = self._read()
                data[aid] = doc
                self._write(data)

    def remove(self, aid):
        if self.col is not None:
            self.col.delete_one({"_id": aid})
        else:
            with self.lock:
                data = self._read()
                data.pop(aid, None)
                self._write(data)
