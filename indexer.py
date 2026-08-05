"""인덱싱 배치: SAP 반출 → bge-m3 임베딩 → Chroma upsert.

사용법:
  python indexer.py --test              # 소량(기본 5건) end-to-end 검증
  python indexer.py --full              # 전체 재인덱싱 (hasMore=false까지)
  python indexer.py --incremental       # 지난 실행 이후 바뀐 유닛만 upsert
  python indexer.py --prog ZTM_STK00    # 특정 프로그램만

유닛 단위 upsert 라서 부분 갱신이 자연스럽다(HNSW). 전체 재벡터화는
임베딩 모델/텍스트 조합 규칙이 바뀔 때만.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import embedder
import sap_client
import store

_STATE_PATH = store._HERE / store.CHROMA_DIR / "index_state.json"


def _load_state() -> dict:
    if _STATE_PATH.exists():
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _index_rows(rows: list[dict], collection, *, batch: int = 64) -> int:
    """행 리스트를 임베딩 후 Chroma에 upsert. 처리 건수 반환."""
    total = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        docs, ids, metas = [], [], []
        for row in chunk:
            text = store.unit_text(row)
            if not text.strip():
                continue  # 벡터화할 내용이 없는 유닛은 건너뜀
            ids.append(store.unit_id(row))
            docs.append(text)
            metas.append(store.unit_metadata(row))
        if not docs:
            continue
        vecs = embedder.embed(docs, batch_size=16)
        collection.upsert(ids=ids, embeddings=vecs, documents=docs, metadatas=metas)
        total += len(docs)
        print(f"  … upsert {total}건 누적", file=sys.stderr)
    return total


def _max_anlz(rows: list[dict], cur_date: str, cur_time: str) -> tuple[str, str]:
    """행들 중 가장 최근 anlzDate/anlzTime 추적 (증분 기준 갱신용)."""
    for r in rows:
        d = str(r.get("anlzDate", "")).replace("-", "")
        t = str(r.get("anlzTime", "")).replace(":", "")
        if (d, t) > (cur_date, cur_time):
            cur_date, cur_time = d, t
    return cur_date, cur_time


def run(mode: str, *, prog: str | None = None, test_n: int = 5) -> None:
    collection = store.get_collection()
    state = _load_state()

    from_date = None
    from_time = None
    if mode == "incremental":
        from_date = state.get("last_anlz_date") or None
        from_time = state.get("last_anlz_time") or None
        print(f"[incremental] IV_FROM_DATE={from_date} IV_FROM_TIME={from_time}", file=sys.stderr)

    if mode == "test":
        page = sap_client.fetch_units(limit=test_n, offset=0, prog=prog)
        rows = (page.get("rows") or [])[:test_n]
        print(f"[test] total={page.get('total')} 반환={len(rows)}건", file=sys.stderr)
    else:
        rows = list(
            sap_client.iter_all_units(from_date=from_date, from_time=from_time, prog=prog)
        )
        print(f"[{mode}] 반출 {len(rows)}건", file=sys.stderr)

    if not rows:
        print("반출된 유닛 없음 — 종료.", file=sys.stderr)
        return

    print(f"임베딩 모델 로드: {embedder.EMBED_MODEL} (device={embedder._pick_device()})", file=sys.stderr)
    n = _index_rows(rows, collection)

    # 증분 기준 시각 갱신 (test 모드는 상태 안 건드림)
    if mode in ("full", "incremental"):
        d = state.get("last_anlz_date", "")
        t = state.get("last_anlz_time", "")
        d, t = _max_anlz(rows, d, t)
        state["last_anlz_date"] = d
        state["last_anlz_time"] = t
        state["last_run_at"] = datetime.now().isoformat(timespec="seconds")
        state["count"] = collection.count()
        _save_state(state)

    print(f"완료: {n}건 인덱싱. 컬렉션 총 {collection.count()}건.", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--test", action="store_true", help="소량 end-to-end 검증")
    g.add_argument("--full", action="store_true", help="전체 재인덱싱")
    g.add_argument("--incremental", action="store_true", help="증분 갱신")
    ap.add_argument("--prog", help="특정 프로그램만")
    ap.add_argument("--n", type=int, default=5, help="test 모드 건수")
    args = ap.parse_args()

    mode = "test" if args.test else "full" if args.full else "incremental"
    run(mode, prog=args.prog, test_n=args.n)


if __name__ == "__main__":
    main()
