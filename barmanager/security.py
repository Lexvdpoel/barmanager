import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                            n=int(n), r=int(r), p=int(p), dklen=len(digest) // 2)
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_for(session_token: str) -> str:
    return hashlib.sha256(b"csrf:" + session_token.encode()).hexdigest()[:40]


class LoginLimiter:
    """Max `limit` failed logins per key within `window` seconds."""

    def __init__(self, limit: int = 5, window: float = 300):
        self.limit, self.window = limit, window
        self._fails: dict[str, deque] = defaultdict(deque)

    def _trim(self, key: str) -> deque:
        q = self._fails[key]
        cutoff = time.time() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        return len(self._trim(key)) >= self.limit

    def fail(self, key: str) -> None:
        self._trim(key).append(time.time())

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)
