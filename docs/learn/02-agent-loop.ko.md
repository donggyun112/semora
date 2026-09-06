# 2. 모델과 도구가 대화하는 과정

[목차](README.ko.md) · [이전](01-dependencies.ko.md)

## 먼저 요청과 실행을 구분하자

모델 응답에 `read(path="memo.txt")`라는 요청이 있다고 해서 파일이 읽힌 것은 아니다.
도구를 등록한 프로그램이 해당 요청을 해석하고 실제 함수를 호출해야 한다.

Pydantic AI에서는 Agent가 이 연결을 진행한다. 사용자는 도구 함수를 등록하고 `run()`을 호출한다.

## 독립 실행 예제: 모델 응답을 직접 정해 보기

다음은 실제 파일이나 외부 API에 접근하지 않는다.
`FunctionModel`로 모델 역할을 하는 함수를 지정한다. 앞 장의 `TestModel`보다 응답을 직접 세밀하게 정할 수 있다.

```python
import asyncio

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel


def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    returned = [
        part
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    if returned:
        return ModelResponse(parts=[TextPart(f"읽은 내용: {returned[-1].content}")])
    return ModelResponse(parts=[
        ToolCallPart("read", {"path": "memo.txt"}, tool_call_id="read-1")
    ])


agent = Agent(FunctionModel(model))


@agent.tool_plain
def read(path: str) -> str:
    """메모 내용을 읽는다."""
    print(f"도구 실행: {path}")
    return "오후 3시 회의"


async def main():
    result = await agent.run("메모 읽어 줘")
    print(result.output)
    for message in result.all_messages():
        print(type(message).__name__, [type(part).__name__ for part in message.parts])


asyncio.run(main())
```

첫 출력은 `도구 실행: memo.txt`, 다음은 `읽은 내용: 오후 3시 회의`다.
뒤에서는 대화 기록의 메시지와 구성 요소 타입을 확인한다.

## messages를 읽는 법

한 메시지에는 여러 `parts`가 들어갈 수 있다. 위 코드의 이중 반복은 모든 메시지의 모든 구성 요소를 검사한다.

| 타입 | 이 예제에서 의미 |
|---|---|
| `ModelRequest` | 모델로 보내는 메시지 |
| `UserPromptPart` | 사용자의 “메모 읽어 줘” |
| `ModelResponse` | 모델이 내놓은 메시지 |
| `ToolCallPart` | 도구 이름·인자·호출 ID |
| `ToolReturnPart` | 도구 실행 결과 |
| `TextPart` | 모델의 텍스트 답변 |

도구 결과도 모델로 보내는 `ModelRequest` 안에 들어간다. `Request`를 “사용자가 직접 입력한 메시지”로만 해석하면 여기서 헷갈린다.
대화 기록 접근은 [공식 Messages 문서](https://pydantic.dev/docs/ai/core-concepts/message-history/)를 참고한다.

## 실제로는 두 번 모델을 부른다

```text
1. 사용자 요청을 넣어 model() 호출
2. 아직 ToolReturnPart가 없으므로 read 호출 요청 반환
3. Agent가 read("memo.txt") 실행
4. 반환값을 ToolReturnPart로 대화 기록에 추가
5. 갱신된 기록으로 model() 다시 호출
6. 이번에는 ToolReturnPart가 있으므로 텍스트 답변 반환
```

`model()`의 두 번째 호출은 첫 번째 함수 호출을 멈춘 자리에서 이어가는 것이 아니다.
새로 받은 메시지를 보고 다시 실행하는 것이다.

이 예제는 시작할 때 이전 대화가 없고 도구 호출이 한 번이라는 전제로 작성했다.
과거 대화에 도구 결과가 이미 있는 범용 챗봇의 판단 규칙으로 그대로 사용하지 않는다.
`FunctionModel` 자체는 [공식 API](https://pydantic.dev/docs/ai/api/models/function/)에서 제공하는 테스트·개발용 모델이다.

## tool_call_id는 무엇을 연결할까?

`read-1`은 함수 이름이 아니라 **이번 호출의 식별자**다.
같은 `read` 함수를 다시 호출해도 별개의 호출이면 다른 식별자로 구분한다.

```text
read 함수
  ├─ 호출 read-1: memo.txt
  └─ 호출 read-2: todo.txt
```

Semora는 이후 이 호출 ID를 실행 기록과 연결한다. 호출 ID가 왜 필요한지 여기서 기억해 두면 된다.

## Semora는 어디에 들어갈까?

Semora의 프로젝트 계약은 모델·메시지·도구·에이전트 루프를 Pydantic AI에 맡긴다.
Semora는 실행 전 정책, 결과 기록, 승인 대기와 복구를 붙인다.
같은 루프를 새로 만드는 프로젝트로 이해하면 소스가 오히려 어려워진다.
역할 분담은 [README](../../README.md)에 설명되어 있다.

## 확인 질문

**Q. `ToolCallPart(...)`를 생성하는 순간 도구 함수가 실행되는가?**

아니다. 호출 요청 데이터가 만들어진다. 실행은 Agent가 처리한다.

**Q. 도구가 반환한 문자열과 최종 답변은 항상 같은가?**

아니다. 이 예제에서도 도구 결과는 `오후 3시 회의`, 최종 답변은 `읽은 내용: 오후 3시 회의`다.

**Q. 커스텀 진행률 이벤트를 알아야 이 흐름을 이해할 수 있는가?**

아니다. 먼저 요청과 결과가 오가는 이 흐름만 이해하면 된다.

**다음:** [3장 — Semora 예제 한 개 읽기](03-permissions.ko.md)
