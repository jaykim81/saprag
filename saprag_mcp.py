"""SAPRAG 시맨틱 검색 MCP 서버.

Claude Desktop이 이 서버의 툴을 호출해 관련 ABAP 유닛을 검색한다.
답변 생성은 Claude Desktop이 담당 — 이 서버는 '검색기'다 (답변 LLM 없음).

질문도 문서와 동일한 bge-m3로 임베딩해 벡터 공간을 일치시킨다.
"""
from __future__ import annotations

import sys
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

import calltree
import embedder
import indexer
import store

mcp = FastMCP("saprag")

# 컬렉션은 최초 검색 시 lazy 로드 (서버 기동 속도 확보)
_collection = None


def _get_collection():
    global _collection
    if _collection is None:
        _collection = store.get_collection()
    return _collection


def _invalidate_caches():
    """재인덱싱 후 파생 캐시를 비운다 (call-tree 인덱스 등)."""
    try:
        calltree._load_index_cached.cache_clear()
    except Exception:
        pass


def _read_last_indexed():
    """index_state.json 의 마지막 인덱싱 정보(있으면)."""
    import json

    path = store._HERE / store.CHROMA_DIR / "index_state.json"
    if path.exists():
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
            return {"date": st.get("last_anlz_date"), "time": st.get("last_anlz_time"),
                    "run_at": st.get("last_run_at")}
        except Exception:
            return None
    return None


def _build_where(
    prog: Optional[str],
    unit_type: Optional[str],
    prog_prefix: Optional[str],
) -> Optional[dict]:
    """Chroma metadata 필터 조합. 조건 없으면 None."""
    conds = []
    if prog:
        conds.append({"progName": {"$eq": prog}})
    if unit_type:
        conds.append({"unitType": {"$eq": unit_type.upper()}})
    # prog_prefix 는 Chroma가 접두 매칭을 직접 지원하지 않아 후처리로 거른다.
    if not conds:
        return None
    return conds[0] if len(conds) == 1 else {"$and": conds}


@mcp.tool()
def search_units(
    query: str,
    top_k: int = 8,
    prog: Optional[str] = None,
    unit_type: Optional[str] = None,
    prog_prefix: Optional[str] = None,
) -> dict:
    """자연어 질문으로 관련 ABAP 유닛을 시맨틱 검색한다.

    한글 업무용어(예: '받을어음 처리 로직')로 검색 가능. 결과에는 출처
    (progName/unitName/lineFrom~lineTo)가 포함되므로 답변에 인용할 것.

    파라미터:
      - query (필수): 검색할 자연어 질문 또는 키워드
      - top_k (선택, 기본 8): 반환할 유닛 수. 5~10 권장
      - prog (선택): 특정 프로그램명 완전일치 필터 (예: 'ZTM_STK00')
      - unit_type (선택): 유닛 종류 필터 (FORM/METHOD/MODULE/EVENT)
      - prog_prefix (선택): 프로그램명 접두 필터 (예: 'ZTM_')
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query 가 비어 있습니다.", "results": []}

    where = _build_where(prog, unit_type, prog_prefix)
    # prog_prefix 후처리 여유분 확보를 위해 넉넉히 조회
    n_fetch = top_k * 4 if prog_prefix else top_k

    qvec = embedder.embed_one(q)
    res = _get_collection().query(
        query_embeddings=[qvec],
        n_results=max(1, n_fetch),
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]

    results = []
    for _id, doc, meta, dist in zip(ids, docs, metas, dists):
        meta = meta or {}
        if prog_prefix and not str(meta.get("progName", "")).startswith(prog_prefix):
            continue
        results.append(
            {
                "score": round(1.0 - float(dist), 4),  # cosine 거리 → 유사도
                "progName": meta.get("progName"),
                "unitType": meta.get("unitType"),
                "unitName": meta.get("unitName"),
                "className": meta.get("className"),
                "lineFrom": meta.get("lineFrom"),
                "lineTo": meta.get("lineTo"),
                "signature": meta.get("signature"),
                "anlzDate": meta.get("anlzDate"),
                "text": doc,
            }
        )
        if len(results) >= top_k:
            break

    return {"query": q, "returned": len(results), "results": results}


@mcp.tool()
def get_unit_detail(prog: str, unit: str) -> dict:
    """특정 유닛의 전체 상세를 반환한다 (progName + unitName 매칭).

    같은 이름이 여러 개면 모두 반환. search_units 로 찾은 뒤 상세 확인용.
    """
    res = _get_collection().get(
        where={"$and": [{"progName": {"$eq": prog}}, {"unitName": {"$eq": unit}}]},
        include=["documents", "metadatas"],
    )
    out = []
    for doc, meta in zip(res.get("documents") or [], res.get("metadatas") or []):
        m = dict(meta or {})
        m["text"] = doc
        out.append(m)
    return {"prog": prog, "unit": unit, "found": len(out), "units": out}


@mcp.tool()
def get_call_tree(prog: str, unit: str, depth: int = 2) -> dict:
    """특정 유닛의 호출관계(call-tree)를 반환한다.

    유닛이 호출하는 대상(METHOD/PERFORM/FUNCTION)을 소스라인과 함께 트리로
    구성한다. 인덱스 내 유닛으로 해석되는 호출은 재귀로 펼치고(resolved=true),
    표준 SAP 클래스/FM 등 인덱스 밖 대상은 external(resolved=false) 리프로 표시.
    순환 호출은 cycle=true로 끊는다.

    파라미터:
      - prog (필수): 프로그램명 (예: 'ZSFC_FB_COM')
      - unit (필수): 유닛명 (FORM/METHOD/MODULE명, 예: 'MAKE_FIELDCATALOG')
      - depth (선택, 기본 2): 펼칠 최대 단계 (1=직접 호출만, 상한 5)
    """
    return calltree.build_call_tree(prog, unit, depth)


@mcp.tool()
def reindex_incremental() -> dict:
    """지난 인덱싱 이후 SAP에서 바뀐(신규/수정) 유닛만 가져와 벡터DB에 반영한다.

    보통 수 건~수십 건이라 빠르다(수 초). "인덱싱 갱신해줘", "새로 분석된 것
    반영해줘" 같은 요청에 사용. 기준 시각은 index_state.json에 자동 관리된다.
    반환: {fetched, indexed, total}.
    """
    result = indexer.run("incremental")
    _invalidate_caches()
    return {"ok": True, **result}


@mcp.tool()
def reindex_program(prog: str) -> dict:
    """특정 프로그램의 분석 완료 유닛을 다시 인덱싱한다 (해당 프로그램만).

    "ZTM_STK00 다시 인덱싱해줘"처럼 한 프로그램만 갱신할 때 사용. 범위가
    프로그램 하나로 한정돼 안전하다. 전역 증분 기준 시각은 건드리지 않는다.
    반환: {fetched, indexed, total}.
    """
    if not (prog or "").strip():
        return {"ok": False, "error": "prog(프로그램명)가 필요합니다."}
    result = indexer.run("full", prog=prog.strip(), update_state=False)
    _invalidate_caches()
    return {"ok": True, **result}


@mcp.tool()
def reindex_full(confirm: bool = False) -> dict:
    """전체 유닛을 재임베딩·재인덱싱한다 (비용이 큰 작업).

    ⚠️ 모든 유닛을 다시 임베딩하므로 데이터가 많으면 수 분 이상 걸린다.
    보통은 reindex_incremental 로 충분하며, 전체 재인덱싱은 임베딩 모델이나
    텍스트 조합 규칙이 바뀌었을 때만 필요하다.

    실수 방지를 위해 confirm=True 일 때만 실행한다. confirm 없이 호출하면
    현재 인덱스 규모와 함께 안내만 반환한다.
    """
    if not confirm:
        cnt = _get_collection().count()
        return {
            "ok": False,
            "needs_confirm": True,
            "message": (
                f"전체 재인덱싱은 현재 약 {cnt}건을 모두 다시 임베딩합니다(수 분 소요 가능). "
                "정말 실행하려면 confirm=True 로 다시 호출하세요. "
                "단순 최신화라면 reindex_incremental 을 쓰세요."
            ),
        }
    result = indexer.run("full")
    _invalidate_caches()
    return {"ok": True, **result}


@mcp.tool()
def prune_deleted_units(confirm: bool = False) -> dict:
    """SAP에서 삭제(또는 완료상태 해제)된 유닛의 '유령 벡터'를 정리한다.

    삭제는 타임스탬프로 감지되지 않으므로, SAP 현재 키 전체와 벡터DB 키를
    대조해 벡터DB에만 남은 것을 찾는다(SAP 전체 페이징 1회 필요, 임베딩 없음).

    confirm=False(기본): 유령 벡터가 몇 건인지 미리보기만 반환(삭제 안 함).
    confirm=True: 실제 삭제. "유령 벡터 정리해줘"에 사용. 주기적(예: 주 1회) 권장.
    """
    result = indexer.prune_deleted(do_delete=confirm)
    if confirm:
        _invalidate_caches()
        result["ok"] = True
    else:
        n = result.get("ghost_count", 0)
        result["needs_confirm"] = n > 0
        result["message"] = (
            f"유령 벡터 {n}건 발견. 삭제하려면 confirm=True 로 다시 호출하세요."
            if n else "유령 벡터 없음 — 정리 불필요."
        )
    return result


@mcp.tool()
def index_stats() -> dict:
    """인덱스 현황 (총 유닛 수, 컬렉션명, 마지막 인덱싱 시각)."""
    col = _get_collection()
    return {
        "collection": store.CHROMA_COLLECTION,
        "count": col.count(),
        "embed_model": embedder.EMBED_MODEL,
        "last_indexed": _read_last_indexed(),
    }


if __name__ == "__main__":
    print("[saprag] MCP 서버 기동 (stdio). 툴: search_units, get_unit_detail, "
          "get_call_tree, reindex_incremental, reindex_program, reindex_full, "
          "prune_deleted_units, index_stats", file=sys.stderr)
    mcp.run()
