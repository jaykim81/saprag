# SAPRAG — SAP 소스분석 시맨틱 검색 MCP 서버

SAP에 쌓인 ABAP 소스 분석 결과(유닛 단위 LLM 요약)를 **로컬 임베딩(bge-m3)으로
벡터화 → Chroma에 저장**하고, **Claude Desktop이 MCP로 자연어 검색**하는
"시맨틱 검색 MCP 서버"입니다.

> 핵심 원칙: 이 서버는 **검색기(Retriever)**입니다. 답변 생성(Generation)은
> Claude Desktop이 담당하므로 여기에 답변용 LLM을 두지 않습니다. 임베딩은
> 로컬 모델(bge-m3)로만 수행 — 외부 임베딩 API 없음(보안 + 무료).

---

## 목차
1. [무엇을 하는가](#무엇을-하는가)
2. [아키텍처 / 데이터 흐름](#아키텍처--데이터-흐름)
3. [요구 사항](#요구-사항)
4. [설치](#설치)
5. [설정 (.env)](#설정-env)
6. [인덱싱](#인덱싱)
7. [Claude Desktop 등록](#claude-desktop-등록)
8. [MCP 툴](#mcp-툴)
9. [증분 갱신 전략](#증분-갱신-전략)
10. [파일 구조](#파일-구조)
11. [트러블슈팅](#트러블슈팅)
12. [설계 원칙 / 주의사항](#설계-원칙--주의사항)

---

## 무엇을 하는가

SAP 테이블 `ZSPAIT_PRG_ANLZ`에는 커스텀 ABAP 프로그램이 **유닛
(FORM/METHOD/MODULE/EVENT) 단위**로 쪼개져 있고, 각 유닛마다 LLM이 생성한
요약·로직설명·입출력·업무의미·SQL·호출관계가 저장돼 있습니다.

SAPRAG는 이 데이터를 가져와 벡터화하고, 개발자·현업이 **자연어로**
질문하면 관련 유닛을 찾아줍니다.

```
질문: "받을어음 처리 로직 어디 있어?"
   → search_units("받을어음 처리")
   → ZSFR_0010/F4_BOE_UMSKZ, ZSFR_2110/MAKE_ISBOE … (출처 라인 포함)
```

키워드 검색이 아니라 **의미 검색**이라 "받을어음 ≈ BOE ≈ 어음"처럼 표현이
달라도 찾습니다. (한글 업무용어 ↔ 영문 코드 매칭이 핵심이라 다국어 모델
bge-m3를 씁니다.)

---

## 아키텍처 / 데이터 흐름

```
[인덱싱 — 배치, 1회 + 증분]
  SAP ZSPAIT_PRG_ANLZ
    │  call_func OData 액션으로 반출 FM(ZSPDEV_RAG_PRG_ANLZ) 호출
    ▼
  sap_client.py  ─ 페이징·증분·CSRF 처리, {total, rows} 파싱
    │
    ▼
  embedder.py    ─ bge-m3 임베딩 (Apple Silicon MPS 가속, 1024차원)
    │
    ▼
  store.py       ─ 유닛→(ID/문서텍스트/메타데이터) 매핑
    │
    ▼
  Chroma (chroma_db/)  ─ cosine, upsert (증분 자연스러움)

[질문 — 실시간]
  Claude Desktop
    │  MCP 툴 호출
    ▼
  saprag_mcp.py  ─ 질문을 같은 bge-m3로 임베딩 → Chroma top-k 검색
    │
    ▼
  유닛 몇 건(텍스트+메타데이터+출처) 반환
    │
    ▼
  Claude Desktop 이 조합해 답변 + 출처(PROG/UNIT/LINE)
```

**벡터화 대상 텍스트** = `summary` + `logicDesc` + `ioDesc` + `bizDesc` (+ `sqlText`)
를 라벨 붙여 결합 → 이 조합으로 임베딩 (규칙은 `store.unit_text`).

**문서 ID** = `progName|includeName|unitType|className|unitName|zseq` (유닛 고유키).
같은 키면 upsert가 덮어쓰므로 재실행이 안전합니다.

---

## 요구 사항

- **Apple Silicon Mac** (M-시리즈) — MPS(Metal) 가속. CPU/CUDA로도 동작(자동 감지).
- **Python 3.11+** (개발·검증: 3.13)
- 디스크 약 **5~6GB**: bge-m3 모델 ~2GB(첫 실행 시 다운로드) + 패키지(torch 등)
  2~4GB + Chroma 데이터 ~0.1GB. 전부 이 폴더에 자체 포함.
- SAP 접속 정보 (아래 `.env`)

---

## 설치

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

> **mcp 버전 주의**: `mcp[cli]`는 **1.x**로 고정돼 있습니다(`>=1.2.0,<2`).
> 2.0.0에서 `FastMCP`가 `MCPServer`로 개명되고 `mcp.server.fastmcp` 경로가
> 사라졌기 때문입니다.

첫 임베딩 실행 시 bge-m3(~2GB)가 `./.hf_cache/`로 자동 다운로드됩니다(1회, 인터넷 필요).

```bash
./.venv/bin/python embedder.py   # 모델 다운로드 + MPS 임베딩 동작 확인
```

---

## 설정 (.env)

`.env.example`를 복사해 값을 채웁니다. **인증정보는 절대 코드/깃에 넣지 않습니다**
(`.env`는 `.gitignore` 대상).

```bash
cp .env.example .env
chmod 600 .env
```

| 키 | 설명 |
|---|---|
| `SAP_BASE` | SAP 호스트:포트 (예: `https://host:44360`) |
| `SAP_USER` / `SAP_PASS` | SAP 계정 |
| `SAP_SERVICE_PATH` | OData4 서비스 경로 (호스트 뒤, 끝 슬래시 없음) |
| `SAP_ENTITY` | 엔티티셋 이름 (기본 `Tool`) |
| `SAP_ACTION_NAMESPACE` | `call_func` 액션 네임스페이스 |
| `SAP_VERIFY_SSL` | 사설 인증서면 `false` |
| `SAP_TIMEOUT` | HTTP 타임아웃(초) |
| `RAG_FETCH_FUNC` | 반출 FM명 (기본 `ZSPDEV_RAG_PRG_ANLZ`) |
| `EMBED_MODEL` | 임베딩 모델 (기본 `BAAI/bge-m3`) |
| `CHROMA_DIR` | Chroma 저장 폴더 (기본 `chroma_db`) |
| `CHROMA_COLLECTION` | 컬렉션 이름 (기본 `sap_units`) |
| `RAG_PAGE_LIMIT` | 반출 페이지 크기 (상한 500) |

기존 SAP MCP 서버(`../SAPMCP/.env`)의 `SAP_*` 값을 재사용할 수 있습니다.

---

## 인덱싱

```bash
# 소량 end-to-end 검증 (기본 5건, --n 으로 조정)
./.venv/bin/python indexer.py --test --n 8

# 전체 재인덱싱 (hasMore=false 까지 페이징)
./.venv/bin/python indexer.py --full

# 증분 — 지난 실행 이후 바뀐 유닛만 upsert
./.venv/bin/python indexer.py --incremental

# 특정 프로그램만 부분 인덱싱
./.venv/bin/python indexer.py --full --prog ZTM_STK00
```

- `IV_STATUS='C'`(분석 완료분)만 반출 → 미완성 유닛 벡터화 방지.
- 증분 기준 시각(마지막 anlzDate/anlzTime)은 `chroma_db/index_state.json`에 기록.
- 소스 분석이 진행 중이면(예: 현재 900여 건 → 최종 2만) 데이터가 찬 뒤 `--full`,
  이후 주기적으로 `--incremental` 권장.

---

## Claude Desktop 등록

`claude_desktop_config.json`의 `mcpServers`에 추가 (경로는 **절대경로**, 공백 포함 OK):

```json
"saprag": {
  "command": "/절대경로/SAPRAG/.venv/bin/python",
  "args": ["/절대경로/SAPRAG/saprag_mcp.py"]
}
```

macOS 설정 파일 위치: `~/Library/Application Support/Claude/claude_desktop_config.json`

수정 후 **Claude Desktop 재시작** → 자연어로 검색.

---

## MCP 툴

### `search_units(query, top_k=8, prog=None, unit_type=None, prog_prefix=None)`
자연어 질문으로 관련 유닛을 시맨틱 검색. 결과에 **출처(progName/unitName/lineFrom~lineTo)**
와 유사도 score 포함.

- `top_k`: 반환 개수 (5~10 권장)
- `prog`: 프로그램명 완전일치 필터 (예: `ZTM_STK00`)
- `unit_type`: `FORM`/`METHOD`/`MODULE`/`EVENT` 필터
- `prog_prefix`: 프로그램명 접두 필터 (예: `ZTM_` — 후처리 매칭)

```json
{ "returned": 3, "results": [
  { "score": 0.739, "progName": "ZTM_FXT00", "unitName": "F_GET_KOSTL_TX",
    "unitType": "FORM", "lineFrom": 5287, "lineTo": 5299, "text": "[요약] …" }
]}
```

### `get_unit_detail(prog, unit)`
특정 유닛의 전체 상세(합쳐진 문서 텍스트 + 메타데이터) 반환. 같은 이름이
여럿이면 모두 반환.

### `get_call_tree(prog, unit, depth=2)`
유닛의 **호출관계(call-tree)**를 소스라인과 함께 트리로 구성.
`callRef` 메타데이터를 파싱해 대상을 인덱스 내 유닛으로 해석(resolve)하고 재귀 확장.

- `depth`: 펼칠 최대 단계 (1=직접 호출만, 기본 2, 상한 5)
- 각 호출 노드: `callKind`(METHOD/PERFORM/FUNCTION), `callTgt`, `srcLine`,
  `resolved`(인덱스 내 해석 여부), `cycle`(순환 시 중단), `truncated`(depth 한계)
- 표준 SAP 클래스/FM 등 인덱스 밖 대상은 `resolved:false` external 리프.

```json
{ "found": 1, "depth": 2, "trees": [
  { "progName":"ZSFR_2110", "unitName":"AT SELECTION-SCREEN", "unitType":"EVENT",
    "calls":[ { "callKind":"PERFORM", "callTgt":"SCREEN_PAI", "srcLine":28,
                "resolved":true, "child": { "unitName":"SCREEN_PAI", "calls":[…] } } ] }
]}
```

### `index_stats()`
인덱스 현황 — 총 유닛 수, 컬렉션명, 임베딩 모델.

---

## 증분 갱신 전략

벡터화는 **유닛(행) 단위로 독립적**이라, 바뀐 유닛만 다시 임베딩해 upsert하면
됩니다. Chroma는 HNSW라 부분 upsert가 즉시 반영 — 전체 재인덱싱 불필요.

| 경우 | 처리 |
|---|---|
| **수정된 유닛** | anlzDate 갱신 → 반출 → 같은 키로 upsert(덮어쓰기) |
| **신규 유닛** | 새 행 → 반출 → 삽입 |
| **삭제된 유닛** | ⚠️ anlzDate로 안 잡힘 → 유령 벡터 잔존. 주기적으로 SAP 현재 키 ↔ Chroma 키 비교 후 정리(추후 스크립트). |

**전체 재벡터화가 필요한 드문 경우**: 임베딩 모델 교체, 또는 벡터화 대상 텍스트
조합 규칙(`store.unit_text`) 변경 시에만. 데이터 몇 건 변경으로는 절대 전체 안 함.

---

## 파일 구조

```
SAPRAG/
├── sap_client.py     # SAP call_func 호출 + 반출 FM 페이징/증분/CSRF
├── embedder.py       # bge-m3 임베딩 (MPS), 모델 캐시 .hf_cache/
├── store.py          # 유닛→(ID/텍스트/메타) 매핑 + Chroma 컬렉션(cosine)
├── calltree.py       # callRef 기반 호출관계 트리 구성
├── indexer.py        # 반출→임베딩→upsert (test/full/incremental)
├── saprag_mcp.py     # MCP 서버 (search_units/get_unit_detail/get_call_tree/index_stats)
├── requirements.txt
├── .env.example      # 설정 템플릿 (실제 .env 는 git 제외)
├── .gitignore
├── chroma_db/        # (생성됨) Chroma 데이터 + index_state.json — git 제외
├── .hf_cache/        # (생성됨) bge-m3 모델 캐시 — git 제외
└── .venv/            # (생성됨) 가상환경 — git 제외
```

---

## 트러블슈팅

- **`ModuleNotFoundError: mcp.server.fastmcp`** → mcp 2.x가 설치됨.
  `pip install "mcp[cli]>=1.2.0,<2"` 로 1.x 재설치.
- **`call_func 응답에서 반출 JSON을 찾지 못함`** → 이 서비스는 반출 결과를 최상위
  `reponse`(오탈자 그대로, 소문자) 문자열 필드에 담습니다. `sap_client._extract_payload`
  가 처리하지만, 서비스가 필드명을 바꾸면 이 함수에 후보 키를 추가.
- **HF 다운로드가 느림** → 무인증 요청 rate limit. `HF_TOKEN` 설정 시 빨라짐.
  캐시(`.hf_cache/`) 후에는 즉시 로드.
- **MPS 미사용** → `embedder.py` 실행 시 `device=mps` 로그 확인. `torch.backends.mps.
  is_available()` 가 False면 CPU로 자동 폴백(느리지만 동작).
- **SAP 401/403** → CSRF 토큰 만료. `sap_client`가 자동 재발급 후 1회 재시도.
  계속되면 `.env` 계정·경로 확인.

---

## 설계 원칙 / 주의사항

- ✅ **검색기만** 만든다. 답변 생성은 Claude Desktop.
- ✅ 질문·문서 임베딩은 **동일 bge-m3** (벡터 공간 일치 필수).
- ✅ 외부 임베딩 API 미사용 (로컬·무료·보안).
- ✅ 인증정보는 `.env`로, 코드/깃에 하드코딩 금지.
- ✅ 증분 upsert가 기본. 전체 재벡터화는 규칙 변경 시에만.
- ⚠️ 반출 FM(`ZSPDEV_RAG_PRG_ANLZ`)은 **MCP 툴로 노출하지 않음**(대량반출 오호출
  방지). 인덱싱 배치가 `call_func`로 직접 호출.
