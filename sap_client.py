"""SAP 접속 클라이언트 — RAG 인덱싱 배치 전용.

기존 SAP MCP 서버(../SAPMCP/sap_mcp.py)의 검증된 call_func / CSRF 로직을
그대로 재사용한다. 여기서는 ZSPDEV_RAG_PRG_ANLZ 반출 FM을 call_func 경유로
직접 호출하기만 한다 (MCP 툴로는 노출하지 않음 — 의도적).

인증정보는 .env 에서만 읽는다. 코드에 하드코딩하지 않는다.
"""
from __future__ import annotations

import json
import os
import warnings
from pathlib import Path
from typing import Any, Iterator, Optional

import httpx
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f".env 에 {name} 가 설정되어 있어야 합니다.")
    return val or ""


SAP_BASE = _env("SAP_BASE", required=True)
SAP_USER = _env("SAP_USER", required=True)
SAP_PASS = _env("SAP_PASS", required=True)
SAP_SERVICE_PATH = _env(
    "SAP_SERVICE_PATH",
    "/sap/opu/odata4/sap/zspaiv_r_tool_info_api_v4"
    "/srvd_a2x/sap/zspaiv_r_tool_info_api/0001",
)
SAP_ENTITY = _env("SAP_ENTITY", "Tool")
SAP_ACTION_NAMESPACE = _env(
    "SAP_ACTION_NAMESPACE",
    "com.sap.gateway.srvd_a2x.zspaiv_r_tool_info_api.v0001",
)
SAP_VERIFY_SSL = _env("SAP_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
SAP_TIMEOUT = float(_env("SAP_TIMEOUT", "120"))

RAG_FETCH_FUNC = _env("RAG_FETCH_FUNC", "ZSPDEV_RAG_PRG_ANLZ")
RAG_PAGE_LIMIT = int(_env("RAG_PAGE_LIMIT", "300"))

_ROOT = f"{SAP_BASE.rstrip('/')}{SAP_SERVICE_PATH}"
# CSRF 토큰 발급용 — 가벼운 GET (엔티티셋 $top=1). 필터 불필요.
CSRF_URL = f"{_ROOT}/{SAP_ENTITY}?$top=1"
CALL_FUNC_URL = f"{_ROOT}/{SAP_ENTITY}/{SAP_ACTION_NAMESPACE}.call_func"

if not SAP_VERIFY_SSL:
    warnings.filterwarnings("ignore", message=".*verify=False.*")
    warnings.filterwarnings("ignore", message=".*Unverified HTTPS.*")

_http = httpx.Client(auth=(SAP_USER, SAP_PASS), verify=SAP_VERIFY_SSL, timeout=SAP_TIMEOUT)
_csrf_token: Optional[str] = None


def _fetch_csrf() -> str:
    r = _http.get(CSRF_URL, headers={"x-csrf-token": "fetch"})
    r.raise_for_status()
    token = r.headers.get("x-csrf-token")
    if not token:
        raise RuntimeError("SAP가 CSRF 토큰을 반환하지 않았습니다.")
    return token


def call_func(object_name: str, params: dict[str, Any]) -> Any:
    """SAP call_func 액션으로 함수 모듈을 동적 호출하고 OData 응답(dict)을 반환."""
    global _csrf_token
    if _csrf_token is None:
        _csrf_token = _fetch_csrf()

    body = {
        "OBJECT_NAME": object_name,
        "PROMPT": "",
        "_PARAMETERS": [
            {
                "PARAMETER_NAME": k,
                "PARAMETER_VALUE": (
                    json.dumps(v, ensure_ascii=False)
                    if isinstance(v, (list, dict))
                    else str(v)
                ),
            }
            for k, v in params.items()
            if v is not None and v != ""
        ],
    }

    def _post():
        return _http.post(
            CALL_FUNC_URL,
            headers={"x-csrf-token": _csrf_token or "", "Content-Type": "application/json"},
            json=body,
        )

    r = _post()
    if r.status_code in (401, 403):  # 토큰 만료 → 재발급 후 1회 재시도
        _csrf_token = _fetch_csrf()
        r = _post()
    if r.status_code >= 400:
        raise RuntimeError(f"SAP {r.status_code}: {r.text[:2000]}")
    return r.json()


def _extract_payload(odata_resp: Any) -> dict:
    """call_func OData 응답에서 실제 반출 JSON({total, rows, ...})을 꺼낸다.

    이 서비스는 반출 FM 결과를 최상위 'reponse'(오탈자 그대로) 문자열 필드에
    담아 반환한다. 표기 변형(response/EV_FUNCTION_RESPONSE)과 {"value":{...}}
    래핑도 함께 방어한다.
    """
    node = odata_resp
    if isinstance(node, dict) and "value" in node and isinstance(node["value"], dict):
        node = node["value"]

    if isinstance(node, dict):
        for key in ("reponse", "response", "EV_FUNCTION_RESPONSE",
                    "ev_function_response", "EV_RESPONSE"):
            if key in node and node[key] is not None:
                raw = node[key]
                if isinstance(raw, str):
                    return json.loads(raw)
                if isinstance(raw, dict):
                    return raw
        # 이미 반출 형태({total, rows})가 최상위에 온 경우
        if "rows" in node or "total" in node:
            return node
    raise RuntimeError(
        "call_func 응답에서 반출 JSON을 찾지 못함. 실제 구조:\n"
        + json.dumps(odata_resp, ensure_ascii=False)[:1500]
    )


def fetch_units(
    *,
    status: str = "C",
    limit: int = RAG_PAGE_LIMIT,
    offset: int = 0,
    from_date: str | None = None,
    from_time: str | None = None,
    prog: str | None = None,
) -> dict:
    """반출 FM 1페이지 호출 → {total, offset, limit, returned, hasMore, rows}."""
    params = {
        "IV_STATUS": status,
        "IV_LIMIT": str(min(int(limit), 500)),  # FM 상한 500
        "IV_OFFSET": str(offset),
        "IV_FROM_DATE": from_date,
        "IV_FROM_TIME": from_time,
        "IV_PROG": prog,
    }
    return _extract_payload(call_func(RAG_FETCH_FUNC, params))


def iter_all_units(
    *,
    status: str = "C",
    page_limit: int = RAG_PAGE_LIMIT,
    from_date: str | None = None,
    from_time: str | None = None,
    prog: str | None = None,
) -> Iterator[dict]:
    """hasMore=false 까지 페이징하며 모든 유닛(행)을 하나씩 yield."""
    offset = 0
    while True:
        page = fetch_units(
            status=status, limit=page_limit, offset=offset,
            from_date=from_date, from_time=from_time, prog=prog,
        )
        rows = page.get("rows") or []
        for row in rows:
            yield row
        if not page.get("hasMore") or not rows:
            break
        offset += len(rows)
