"""Chroma 저장소 + 유닛→(id, 문서텍스트, 메타데이터) 매핑 규칙.

인덱서와 MCP 검색 서버가 공유한다. 문서 ID·텍스트 조합·메타데이터 규칙을
한 곳에 두어 "질문/문서 임베딩 규칙 일치"를 코드 수준에서 보장한다.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

CHROMA_DIR = os.environ.get("CHROMA_DIR", "chroma_db")
CHROMA_COLLECTION = os.environ.get("CHROMA_COLLECTION", "sap_units")

_CHROMA_PATH = _HERE / CHROMA_DIR

# 유닛 고유키 구성 필드 (문서 ID 조합용)
_KEY_FIELDS = ("progName", "includeName", "unitType", "className", "unitName", "zseq")
# 벡터화 대상 텍스트 필드 (이 순서로 라벨 붙여 합침)
_TEXT_FIELDS = (
    ("summary", "요약"),
    ("logicDesc", "로직"),
    ("ioDesc", "입출력"),
    ("bizDesc", "업무"),
    ("sqlText", "SQL"),
)
# Chroma metadata 로 저장할 필드 (필터·출처·증분용). 스칼라만 허용됨.
_META_FIELDS = (
    "progName", "includeName", "unitType", "className", "unitName", "zseq",
    "lineFrom", "lineTo", "callLevel", "execSeq", "execPath",
    "signature", "anlzDate", "anlzTime", "model",
)


def unit_id(row: dict) -> str:
    """유닛 고유 문서 ID (키 필드 조합)."""
    return "|".join(str(row.get(f, "")) for f in _KEY_FIELDS)


def unit_text(row: dict) -> str:
    """임베딩 대상 텍스트 조합 (라벨 + 값). 값이 있는 필드만 포함."""
    parts = []
    for field, label in _TEXT_FIELDS:
        val = row.get(field)
        if val:
            parts.append(f"[{label}] {str(val).strip()}")
    return "\n".join(parts)


def unit_metadata(row: dict) -> dict:
    """Chroma metadata (None 제외, 스칼라만)."""
    meta: dict[str, Any] = {}
    for f in _META_FIELDS:
        v = row.get(f)
        if v is None or v == "":
            continue
        if isinstance(v, (str, int, float, bool)):
            meta[f] = v
        else:
            meta[f] = str(v)
    # callRef(JSON 문자열)는 별도 보관 — 나중 call-tree 툴용
    if row.get("callRef"):
        meta["callRef"] = str(row["callRef"])
    return meta


def get_collection():
    """PersistentClient 컬렉션. 임베딩은 외부(embedder)에서 넣으므로
    embedding_function 없이 생성하고, cosine 공간으로 설정."""
    import chromadb

    client = chromadb.PersistentClient(path=str(_CHROMA_PATH))
    return client.get_or_create_collection(
        name=CHROMA_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )
