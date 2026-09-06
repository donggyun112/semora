# Semora를 읽기 위한 Pydantic AI 기초

목표는 **Agent 생성 → run() → 도구 실행 → output**을 이해하는 것이다.
스트리밍, 커스텀 이벤트, 그래프 순회는 나중에 읽어도 된다.

## 1. Agent는 무엇인가?

`Agent`는 사용할 모델, 지침, 도구를 묶고 실행을 진행하는 객체다.
생성만으로 모델을 호출하지는 않는다. `run()`에 요청을 전달하면 실행이 시작된다.

아래 예제는 API 키 없이 실행할 수 있다. `TestModel`은 실제 LLM 대신 정해진 동작을 하는 테스트용 모델이다.

```python
import asyncio

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

agent = Agent(
    TestModel(custom_output_text="안녕하세요!"),
    instructions="한국어로 간단하게 답하세요.",
)

async def main():
    result = await agent.run("인사해 줘")
    print(result.output)  # 안녕하세요!

asyncio.run(main())
```

처음에는 프로젝트 루트에서 `uv sync --dev`로 환경을 준비한다.
예제를 Python 파일로 저장하고 `uv run python 파일경로.py`로 실행한다.

| 코드 | 의미 |
|---|---|
| `Agent(...)` | 에이전트 설정 |
| `instructions=...` | 개발자가 모델에 주는 지침 |
| `agent.run("인사해 줘")` | 이번 실행의 사용자 요청 |
| `await` | 비동기 작업이 끝날 때까지 현재 함수를 기다리게 함 |
| `result` | 실행 결과 객체 |
| `result.output` | 최종 답변. 기본 출력 타입은 `str` |
| `asyncio.run(main())` | 일반 Python 스크립트에서 비동기 함수 실행 |

이 테스트 모델의 답변은 미리 지정했다. 지침이나 요청을 이해해서 생성한 답변은 아니다.
실제 서비스에서는 사용할 모델로 교체하고 해당 제공자의 SDK와 인증을 설정한다.

`output_type=int`처럼 출력 타입을 지정할 수도 있지만, 처음에는 기본 문자열만 사용하면 된다.
Pydantic AI의 기본 `Agent` 객체는 여러 실행에 재사용할 수 있다.

근거: [공식 Agent 문서 — Introduction / Running Agents](https://pydantic.dev/docs/ai/core-concepts/agent/).

## 2. 도구를 사용하면 어떤 순서로 실행될까?

일반적인 도구 호출 흐름은 다음과 같다.

```text
사용자 요청
  → Agent가 모델에 요청과 도구 정보를 전달
  → 모델이 도구 이름과 인자를 요청
  → Agent가 인자를 검증하고 Python 함수를 실행
  → 도구 반환값을 모델에 전달
  → 모델이 최종 답변하거나 추가 도구를 요청
  → 실행이 완료되면 result.output 확인
```

도구를 등록했다고 매번 호출하는 것은 아니다. 실제 모델의 응답에 따라 호출 여부가 달라진다.

## 3. `@agent.tool_plain`과 `@agent.tool`

둘 다 Python 함수를 도구로 등록한다. 차이는 **실행 문맥인 `RunContext`를 받는가**다.

| 데코레이터 | 함수 형태 | 선택 기준 |
|---|---|---|
| `@agent.tool_plain` | `fn(일반 인자...)` | 문맥 없이 작업 가능 |
| `@agent.tool` | `fn(ctx: RunContext, 일반 인자...)` | 실행에 전달한 데이터 등이 필요 |

이 구분은 동기·비동기 구분이 아니다. 양쪽 모두 `def`와 `async def`를 사용할 수 있다.
데코레이터는 함수를 등록하며, 그 자리에서 함수 본문을 실행하지 않는다.

## 4. `RunContext`와 `deps`를 함께 이해하기

`RunContext`는 프레임워크가 도구에 전달하는 실행 문맥이다.
우선 **`ctx.deps`로 애플리케이션이 제공한 데이터에 접근한다**는 것만 익히면 된다.

다음은 독립적으로 실행할 수 있는 두 번째 예제다.

```python
import asyncio
from dataclasses import dataclass

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.test import TestModel

@dataclass
class Deps:
    user_name: str

agent = Agent(
    TestModel(call_tools=["service_name", "current_user"]),
    deps_type=Deps,
)

@agent.tool_plain
def service_name() -> str:
    """서비스 이름을 반환한다."""
    return "Semora 학습실"

@agent.tool
def current_user(ctx: RunContext[Deps]) -> str:
    """현재 사용자 이름을 반환한다."""
    return ctx.deps.user_name

async def main():
    result = await agent.run(
        "서비스 이름과 내 이름을 알려 줘",
        deps=Deps(user_name="민수"),
    )
    print(result.output)

asyncio.run(main())
```

`TestModel`은 지정된 두 도구를 호출하고, 기본적으로 도구 결과들을 담은 JSON 문자열을 출력한다.
여기서는 자연스러운 문장 생성보다 도구 연결을 확인한다.
테스트 모델의 동작은 [TestModel 공식 API](https://pydantic.dev/docs/ai/api/models/test/)에서 확인할 수 있다.

```text
run(deps=Deps(user_name="민수"))
             ↓
current_user(ctx)의 ctx.deps
             ↓
ctx.deps.user_name == "민수"
```

`deps_type=Deps`는 데이터의 타입 선언이고, `deps=Deps(...)`는 이번 실행에 쓸 실제 값이다.
`RunContext[Deps]`의 대괄호도 “ctx.deps의 타입은 Deps”라는 뜻이다.

## 5. 모델이 정하는 인자와 앱이 전달하는 데이터

앞 예제의 `agent`에 다음 도구를 등록한다고 생각해 보자. 아래 코드는 설명용 조각이다.

```python
@agent.tool
def greet(ctx: RunContext[Deps], greeting: str) -> str:
    """현재 사용자에게 인사한다."""
    return f"{greeting}, {ctx.deps.user_name}님!"
```

| 값 | 제공하는 쪽 |
|---|---|
| `greeting="안녕하세요"` | 모델의 도구 호출 인자 |
| `ctx` | Pydantic AI |
| `ctx.deps.user_name="민수"` | `run(deps=...)`를 호출한 애플리케이션 |

모델에는 함수 이름, 설명, 인자 스키마가 전달된다. `ctx`는 모델이 채우는 인자에서 제외된다.
도구의 `return`은 모델에 전달할 작업 결과이고, `result.output`은 실행 전체의 최종 답변이다.

근거: [공식 Tool 문서 — Registering via Decorator / Tool Schema](https://pydantic.dev/docs/ai/tools-toolsets/tools/).

## 6. 여기까지 이해했으면 Semora로 넘어가기

이 문서의 import는 `from pydantic_ai import Agent`다.
[Semora의 permissions.py](../examples/permissions.py)는 `from semora import Agent`를 사용한다.
Semora의 클래스 기반 인터페이스까지 모든 사용법이 같다고 생각하지는 말자.

다음 예제에서는 한 가지 질문만 따라가면 된다.

> 모델이 쓰기 도구를 요청했을 때, 실제 실행 전에 누가 허용·거절·승인 대기를 결정할까?

Semora가 추가하는 정책과 승인·복구 기능은 [프로젝트 README](../README.md)에 설명되어 있다.

### 스스로 확인하기

- `Agent(...)` 생성과 `await agent.run(...)`의 차이는?
- `@agent.tool_plain`과 `@agent.tool`의 차이는?
- 모델의 일반 인자와 `ctx.deps`는 각각 누가 제공하는가?
- 도구의 반환값과 `result.output`은 어떻게 다른가?

이 네 가지를 설명할 수 있으면 다음 예제를 읽을 준비가 된 것이다.

이어서 읽을 한국어 자료는 [Semora 학습 목차](learn/README.ko.md)에 있다.
바로 다음은 [도구 인자와 애플리케이션 데이터](learn/01-dependencies.ko.md)다.
이미 요청별 Application 객체를 도구에 전달하고 있다면 `deps`로 바꿔야 하는 것은 아니다.
두 방식의 관계부터 시작해 정책·승인·복구·런타임까지 이어서 읽을 수 있다.
