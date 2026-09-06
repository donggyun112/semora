# Semora

**Pydantic AI 에이전트**를 위한 실행 통제와 이펙트(effect) 복구 장치.

에이전트 루프, 메시지, 모델, 툴은 Pydantic AI가 맡는다. Semora는 그 위에 call-id 원장(ledger), 워커 리스와 펜싱, 승인 대기의 내구성 있는 중단, 재개 시점의 정책 재검증, 그리고 조합 가능한 일곱 개의 제어 지점을 얹는다. 애플리케이션은 Pydantic AI의 `Agent`를 직접 사용한다.

**내구 실행(durable execution)만으로는 왜 부족한가?** 내구 실행은 단계를 재생한다. 하지만 어떤 이펙트가 이미 바깥으로 나갔는지는 말해주지 않고, 멈춰 있던 호출이 돌아왔을 때 정책이 다시 판단하게 만들지도 않는다. Semora가 맡는 게 정확히 이 둘이다. 시작만 하고 결과를 보고하지 않은 툴 호출은 호출자가 "재시도해도 안전하다"고 말하기 전까지 `Indeterminate`로 남는다. 그리고 사람의 승인은 새 정책 판단의 입력일 뿐, 판단 그 자체가 아니다. 원장은 프로토콜이라서 그 아래에 내구 실행 백엔드를 깔 수 있다.

**0.3은 Semora 0.2를 잇는 Pydantic AI 버전이다.** `contribution/pydantic-ai-runtime`에서 개발하던 구현이 이제 Semora 패키지 이름으로 이 저장소에 들어왔다. LangChain 구현은 은퇴했고 Git의 `156e4b1`에 남아 있다. 0.3.0은 호환성을 깨는 릴리스다. [마이그레이션](docs/MIGRATION-0.3.md)과 [API](docs/API.md) 문서를 참고할 것.

## 에이전트 실행하기

이 체크아웃에서 바로:

```bash
uv sync --dev
uv run python examples/reviewer.py
```

```python
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart
from semora import AgentRuntime, MemorySteps, MemoryTranscript


def reply(messages, info):
    return ModelResponse(parts=[TextPart("Hello from Semora")])


agent = Agent(FunctionModel(reply))
runtime = AgentRuntime(MemorySteps(), transcript=MemoryTranscript())
# 비동기 애플리케이션 안에서:
# outcome = await runtime.run("example-run", agent, "hello")
# print(outcome.output)
```

프로바이더 SDK는 선택 사항이다. `semora[openai]`는 Pydantic AI의 OpenAI 호환 프로바이더 지원을 켜고, `semora[postgres]`는 PostgreSQL 어댑터를 설치한다. 메모리 스토어는 테스트와 로컬 실험용이다. 프로세스가 재시작되면 살아남지 못한다.

## Semora가 더하는 것

- **이펙트 기록:** 완료된 툴 호출은 기록해둔 결과를 그대로 재생한다. 시작했지만 보고되지 않은 이펙트는 기본적으로 `Indeterminate`이며, 런타임은 재시도가 안전하리라고 넘겨짚지 않는다.
- **워커 조정:** 실행 리스가 경쟁 워커를 거부하고, 펜싱이 뒤늦은 원장 쓰기를 거부한다. 외부 서비스는 여전히 자기 쪽의 멱등성 또는 정합성 계약이 필요하다.
- **승인 재검증:** `Suspend`는 실행을 세워두고 워커를 놓아준다. `on_resume`은 사람의 답변과 양쪽 정책 버전 레이블을 모두 받아 이펙트를 실행해도 되는지 판단한다.
- **정책 조합:** `on_inputs`, `before_model`, `pre_tool_use`, `post_tool_use`, `before_finish`, `on_resume`, `on_suspend`. 게이트 조합에서는 거부가 중단보다 우선한다.
- **트랜스크립트와 디스패치:** `Prompt`, `Answer`, `Recover`가 내구성 있는 실행 상태를 거쳐 라우팅된다. Pydantic AI의 네이티브 메시지는 그대로 보존된다.

원장의 툴 호출 키는 하나의 실행 범위 안에서만 유효하다. 여러 실행이나 브랜치에 걸쳐 중복을 제거해야 하는 비즈니스 오퍼레이션은 호스트가 소유하는 안정적인 키를 따로 마련해야 한다. `retry_running=True`는 재시도가 안전하다는 명시적 선언이다. post-tool 훅은 완료 표시를 남기기 전에 크래시가 나면 최소 한 번(at least once) 실행되므로, 외부 효과가 있다면 멱등하게 작성해야 한다.

[운영 콘솔](https://github.com/donggyun112/semora-console)은 정책 조합, 요청 범위 결제 중복 제거, 불확정 이펙트, 승인, 정책 분기를 보여준다. 여기 쓰인 결제는 시뮬레이션된 이펙트이며, 이 데모가 특정 외부 결제 프로바이더를 인증하지는 않는다.

## 패키지

| 배포 | 임포트 | 책임 |
|---|---|---|
| `semora` | `semora` | 이펙트 경계, 제어, 런타임, 트랜스크립트와 디스패치 |
| `semora-store` | `semora_store` | 스토리지 프로토콜과 인메모리 구현. 의존성 없음 |
| `semora-store-pg` | `semora_store_pg` | PostgreSQL 어댑터 |

## 개발

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv build --all-packages
```

내구 스토어 적합성 테스트를 돌리려면 `SEMORA_TEST_DSN`에 스크래치 PostgreSQL 데이터베이스를 지정한다. CI는 PostgreSQL을 제공한다. 이 값 없이 로컬에서 돌리면 해당 테스트는 명시적으로 건너뛴다.

MIT 라이선스.
