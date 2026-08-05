"""bge-m3 로컬 임베딩 (Apple Silicon MPS 가속).

- 질문과 문서를 반드시 같은 모델(bge-m3)로 임베딩한다. (벡터 공간 일치)
- 모델 캐시는 프로젝트 안(.hf_cache)에 둔다 → 외장 SSD 자체 포함, 이식 용이.
- 첫 실행 시 ~2GB 모델을 자동 다운로드(1회, 인터넷 필요).
"""
from __future__ import annotations

import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# HF 캐시를 프로젝트 안으로 고정 (import 전에 설정해야 반영됨)
os.environ.setdefault("HF_HOME", str(_HERE / ".hf_cache"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_HERE / ".env")

EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")

_model = None


def _pick_device() -> str:
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def get_model():
    """SentenceTransformer(bge-m3) 싱글턴. 최초 호출 시 로드(메모리 상주)."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(EMBED_MODEL, device=_pick_device())
    return _model


def embed(texts: list[str], *, batch_size: int = 16, normalize: bool = True):
    """텍스트 리스트 → 정규화된 dense 벡터(list[list[float]]).

    normalize=True 로 코사인 유사도가 내적과 일치하게 만든다.
    Chroma 기본 거리(l2)에서도 정규화 벡터면 순위가 코사인과 동일.
    """
    model = get_model()
    vecs = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=normalize,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return vecs.tolist()


def embed_one(text: str) -> list[float]:
    return embed([text])[0]


if __name__ == "__main__":
    import time

    print(f"모델 로드 중: {EMBED_MODEL} (device={_pick_device()}) …")
    t0 = time.time()
    v = embed_one("받을어음 처리 로직")
    print(f"로드+임베딩 완료 {time.time()-t0:.1f}s | dim={len(v)} | head={v[:5]}")
