"""호출관계(call-tree) 구성 — callRef 메타데이터 기반.

각 유닛의 callRef 는 "이 유닛이 호출하는 대상" 목록이다:
  {callSeq, callKind, callTgt, srcLine}
    callKind = METHOD | PERFORM | FUNCTION
    callTgt  = 'CLASS=>METHOD' / 'OBJ->METHOD' (METHOD),
               'FORMNAME [IN PROGRAM X]'        (PERFORM),
               'FUNCMODULE'                     (FUNCTION)

callTgt 를 인덱스 내 유닛으로 해석(resolve)해 트리를 재귀 구성한다.
해석되지 않는 대상(표준 SAP 클래스/FM 등)은 external 리프로 표시.
"""
from __future__ import annotations

import json
from functools import lru_cache

import store

# 트리 크기 안전장치
MAX_DEPTH = 5
MAX_CHILDREN = 60


def _row_id(meta: dict) -> str:
    return store.unit_id(meta)


@lru_cache(maxsize=1)
def _load_index_cached(version: int):
    """(rows_by_id, lookups) 를 구성. version 인자로 캐시 무효화 제어."""
    col = store.get_collection()
    got = col.get(include=["metadatas"])
    metas = got.get("metadatas") or []

    rows: dict[str, dict] = {}
    forms_by_prog: dict[tuple, str] = {}
    forms_global: dict[str, list[str]] = {}
    methods_by_class: dict[tuple, str] = {}
    methods_global: dict[str, list[str]] = {}
    funcs_global: dict[str, list[str]] = {}

    for m in metas:
        rid = _row_id(m)
        rows[rid] = m
        utype = str(m.get("unitType", "")).upper()
        uname = str(m.get("unitName", "")).upper()
        prog = str(m.get("progName", "")).upper()
        cls = str(m.get("className", "")).upper()
        if not uname:
            continue
        if utype in ("FORM", "MODULE", "EVENT"):
            forms_by_prog.setdefault((prog, uname), rid)
            forms_global.setdefault(uname, []).append(rid)
        if utype == "METHOD":
            if cls:
                methods_by_class.setdefault((cls, uname), rid)
            methods_global.setdefault(uname, []).append(rid)
        if utype == "FUNCTION":
            funcs_global.setdefault(uname, []).append(rid)

    return {
        "rows": rows,
        "forms_by_prog": forms_by_prog,
        "forms_global": forms_global,
        "methods_by_class": methods_by_class,
        "methods_global": methods_global,
        "funcs_global": funcs_global,
    }


def _load_index():
    # 컬렉션 건수를 캐시 버전으로 사용 → 재인덱싱 후 자동 갱신
    return _load_index_cached(store.get_collection().count())


def _parse_target(call_kind: str, call_tgt: str) -> dict:
    t = (call_tgt or "").strip()
    kind = (call_kind or "").upper()
    if kind == "METHOD":
        for sep in ("=>", "->"):
            if sep in t:
                left, right = t.split(sep, 1)
                return {"cls": left.strip().upper(), "method": right.strip().split("(")[0].upper()}
        return {"cls": None, "method": t.split("(")[0].upper()}
    if kind == "PERFORM":
        return {"form": t.split()[0].upper() if t else ""}
    if kind == "FUNCTION":
        return {"func": t.split()[0].strip("'\"").upper() if t else ""}
    return {}


def _resolve(call_kind: str, call_tgt: str, src_prog: str, idx: dict) -> str | None:
    """callTgt 를 인덱스 내 유닛 id 로 해석. 불가 시 None."""
    tgt = _parse_target(call_kind, call_tgt)
    kind = (call_kind or "").upper()
    src_prog = (src_prog or "").upper()

    if kind == "PERFORM" and tgt.get("form"):
        rid = idx["forms_by_prog"].get((src_prog, tgt["form"]))
        if rid:
            return rid
        cands = idx["forms_global"].get(tgt["form"], [])
        return cands[0] if len(cands) == 1 else None

    if kind == "METHOD" and tgt.get("method"):
        if tgt.get("cls"):
            rid = idx["methods_by_class"].get((tgt["cls"], tgt["method"]))
            if rid:
                return rid
        cands = idx["methods_global"].get(tgt["method"], [])
        return cands[0] if len(cands) == 1 else None

    if kind == "FUNCTION" and tgt.get("func"):
        cands = idx["funcs_global"].get(tgt["func"], [])
        return cands[0] if len(cands) == 1 else None

    return None


def _node_head(meta: dict) -> dict:
    return {
        "progName": meta.get("progName"),
        "unitType": meta.get("unitType"),
        "className": meta.get("className"),
        "unitName": meta.get("unitName"),
        "lineFrom": meta.get("lineFrom"),
        "lineTo": meta.get("lineTo"),
    }


def _build_node(rid: str, idx: dict, depth: int, on_path: set[str]) -> dict:
    meta = idx["rows"][rid]
    node = _node_head(meta)
    node["calls"] = []

    raw = meta.get("callRef")
    if not raw:
        return node
    try:
        calls = json.loads(raw)
    except Exception:
        return node

    for c in calls[:MAX_CHILDREN]:
        kind = c.get("callKind")
        tgt = c.get("callTgt")
        entry = {
            "callSeq": c.get("callSeq"),
            "callKind": kind,
            "callTgt": tgt,
            "srcLine": c.get("srcLine"),
        }
        child_id = _resolve(kind, tgt, meta.get("progName", ""), idx)
        if not child_id:
            entry["resolved"] = False  # 인덱스 밖(표준 SAP 등)
            entry["child"] = None
        elif child_id in on_path:
            entry["resolved"] = True
            entry["cycle"] = True     # 재귀 순환 → 확장 중단
            entry["child"] = None
        elif depth <= 1:
            entry["resolved"] = True
            entry["truncated"] = True  # depth 한계로 더 안 펼침
            entry["child"] = _node_head(idx["rows"][child_id])
        else:
            entry["resolved"] = True
            entry["child"] = _build_node(child_id, idx, depth - 1, on_path | {child_id})
        node["calls"].append(entry)

    return node


def build_call_tree(prog: str, unit: str, depth: int = 2) -> dict:
    """prog+unit 유닛의 호출 트리를 depth 단계까지 구성해 반환.

    - depth: 펼칠 최대 단계 (1=직접 호출만, 기본 2). 상한 MAX_DEPTH.
    - 해석 안 되는 대상은 resolved=false (external), 순환은 cycle=true.
    """
    depth = max(1, min(int(depth), MAX_DEPTH))
    idx = _load_index()

    prog_u = (prog or "").upper()
    unit_u = (unit or "").upper()
    roots = [
        rid for rid, m in idx["rows"].items()
        if str(m.get("progName", "")).upper() == prog_u
        and str(m.get("unitName", "")).upper() == unit_u
    ]
    if not roots:
        return {"prog": prog, "unit": unit, "found": 0,
                "error": f"'{prog}' / '{unit}' 유닛을 인덱스에서 찾지 못함.", "trees": []}

    trees = [_build_node(rid, idx, depth, {rid}) for rid in roots]
    return {"prog": prog, "unit": unit, "found": len(trees), "depth": depth, "trees": trees}
