"""bge-m3 로컬 임베딩 (Apple Silicon MPS 가속).

- 질문과 문서를 반드시 같은 모델(bge-m3)로 임베딩한다. (벡터 공간 일치)
- 모델 캐시는 프로젝트 안(.hf_cache)에 둔다 → 외장 SSD 자체 포함, 이식 용이.
- 첫 실행 시 ~2GB 모델을 자동 다운로드(1회, 인터넷 필요).
"""
from __future__ import annotations

import gc
import os
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# HF 캐시를 프로젝트 안으로 고정 (import 전에 설정해야 반영됨)
os.environ.setdefault("HF_HOME", str(_HERE / ".hf_cache"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_HERE / ".env")

EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")

# 마지막 사용 후 이 시간(초) 동안 안 쓰면 모델을 내려 메모리(~2GB)를 반환한다. 0 이면 상주.
IDLE_UNLOAD_SEC = int(os.environ.get("EMBED_IDLE_UNLOAD_SEC", "600"))

_model = None
_lock = threading.RLock()  # 로드/인코딩/해제 직렬화 (인코딩 도중 해제 방지)
_last_used = 0.0
_watcher: threading.Thread | None = None


def _pick_device() -> str:
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def get_model():
    """SentenceTransformer(bge-m3) 싱글턴. 최초 호출 시 로드, IDLE_UNLOAD_SEC 유휴 시 해제."""
    global _model, _last_used
    with _lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer

            _model = SentenceTransformer(EMBED_MODEL, device=_pick_device())
            _start_watcher()
        _last_used = time.monotonic()
        return _model


def unload_model() -> bool:
    """모델을 내리고 torch 캐시를 비운다. 내렸으면 True."""
    global _model
    with _lock:
        if _model is None:
            return False
        _model = None
        gc.collect()
        try:
            import torch

            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        return True


def _idle_loop():
    while True:
        time.sleep(min(60, max(IDLE_UNLOAD_SEC, 1)))
        with _lock:
            if _model is not None and time.monotonic() - _last_used >= IDLE_UNLOAD_SEC:
                unload_model()


def _start_watcher():
    global _watcher
    if IDLE_UNLOAD_SEC <= 0 or (_watcher and _watcher.is_alive()):
        return
    _watcher = threading.Thread(target=_idle_loop, name="embed-idle-unload", daemon=True)
    _watcher.start()


def embed(texts: list[str], *, batch_size: int = 16, normalize: bool = True):
    """텍스트 리스트 → 정규화된 dense 벡터(list[list[float]]).

    normalize=True 로 코사인 유사도가 내적과 일치하게 만든다.
    Chroma 기본 거리(l2)에서도 정규화 벡터면 순위가 코사인과 동일.
    """
    global _last_used
    with _lock:
        model = get_model()
        vecs = model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=normalize,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        _last_used = time.monotonic()
    return vecs.tolist()


def embed_one(text: str) -> list[float]:
    return embed([text])[0]


if __name__ == "__main__":
    import time

    print(f"모델 로드 중: {EMBED_MODEL} (device={_pick_device()}) …")
    t0 = time.time()
    v = embed_one("받을어음 처리 로직")
    print(f"로드+임베딩 완료 {time.time()-t0:.1f}s | dim={len(v)} | head={v[:5]}")
