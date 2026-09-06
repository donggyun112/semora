# 4. 정책을 표현하고 조합하기

[목차](README.ko.md) · [이전](03-permissions.ko.md)

## 세 가지 결정을 먼저 익히자

도구 정책은 `Continue`, `Deny`, `Suspend` 중 하나로 답한다.
모델이 판단 결과를 해석해서 실행 여부를 결정하는 구조가 아니다. 런타임이 이 결과를 적용한다.

| 결정 | 뜻 | 해당 도구 본문 |
|---|---|---|
| `Continue()` | 이 정책에서 진행 허용 | 실행 경로를 계속 진행 |
| `Deny(result)` | 호출을 거절하고 대체 결과 제공 | 실행하지 않음 |
| `Suspend(request)` | 외부 응답을 받도록 실행 보류 | 승인 대기 중 실행하지 않음 |

`Continue`라고 무조건 새로 실행하는 것은 아니다. 이미 완료된 호출의 결과 재사용 등은 별도 실행 기록에 따라 결정된다.

## Deny와 Suspend는 왜 다른가?

“저 파일에는 접근할 수 없다”는 현재 결정이 끝난 상태다. `Deny`로 결과를 모델에 전달할 수 있다.

“사람의 확인이 오면 다시 판단하겠다”는 아직 결정에 필요한 입력이 부족한 상태다. `Suspend`로 요청을 보관하고 외부 답변을 기다린다.

`Deny`는 도구 하나의 거절이지 반드시 에이전트 실행 전체의 종료는 아니다.
모델이 거절 결과를 받고 다른 답을 만들 수 있다.
사람이 승인 요청에 직접 거절한 경우는 현재 Semora에서 별도로 처리하며, 다음 장에서 구분한다.

## 여러 정책이 충돌하면 어떻게 할까?

예를 들어 한 정책은 “승인받으면 쓰기 가능”, 다른 정책은 “이 경로는 항상 금지”라고 할 수 있다.
`Permissions`는 **거절을 우선하고, 거절이 없으면 승인 대기를, 둘 다 없으면 허용을 선택**한다.

```text
Continue + Continue → Continue
Continue + Suspend  → Suspend
Suspend  + Deny     → Deny
Deny     + Continue → Deny
```

순서대로 평가하되 `Continue`만 보고 끝내지 않는다. `Suspend`를 만나도 뒤에 거절 정책이 있는지 더 본다.
`Deny`를 만나면 즉시 반환한다. 여러 `Suspend`가 나오면 첫 요청을 유지한다.

## 독립 실행 예제: 조합만 따로 확인하기

모델 없이도 정책 조합을 실험할 수 있다.

```python
import asyncio

from pydantic_ai.messages import ToolCallPart
from semora import Continue, Ctx, Deny, Permissions, Suspend


async def allow(ctx, call):
    return Continue()


async def ask(ctx, call):
    return Suspend({"pending_id": "approval-1"})


async def reject(ctx, call):
    return Deny("이 경로는 수정할 수 없습니다")


async def main():
    ctx = Ctx(turn=0)
    call = ToolCallPart("write", {"path": "config.txt"}, tool_call_id="c1")
    for stages in [(allow, allow), (allow, ask), (ask, reject), (reject, ask)]:
        decision = await Permissions(*stages)(ctx, call)
        print(type(decision).__name__)


asyncio.run(main())
```

출력은 `Continue`, `Suspend`, `Deny`, `Deny` 순서다.
이 실험은 정책을 직접 호출한다. 승인 상태를 저장하거나 도구를 실행하는 실험은 아니다.

## 정책과 실행을 나누면 얻는 것

정책은 “무슨 결정을 했는지”를 반환하고, 실행 계층은 그 결정을 실제 실행·보류·기록으로 연결한다.
그래서 모델을 켜지 않고도 정책 충돌을 테스트할 수 있다.

이것 역시 애플리케이션에서 직접 만들 수 있는 구조다.
Semora를 사용하는 선택은 결정 타입과 적용 위치, 승인·복구 연결 규칙을 공통 구현으로 사용하는 선택이다.

## ControlPlane은 정책 함수들을 연결하는 객체다

다음은 연결 방식만 보여 주는 부분 코드다.

```python
controls = ControlPlane(
    pre_tool_use=Permissions(allow, ask),
    on_resume=recheck_current_policy,
)
```

`Permissions`는 한 제어 지점 안에서 여러 판단을 합친다.
`ControlPlane`은 서로 다른 제어 지점에 함수를 배치한다.
따로 설정하지 않은 지점에 애플리케이션의 권한 규칙이 저절로 생기지는 않는다.

## 일곱 제어 지점은 이름만 먼저 연결하자

| 지점 | 질문 | 반환 형태 |
|---|---|---|
| `on_inputs` | 입력을 수정·제외할까? | 입력 목록 또는 `Halt` |
| `before_model` | 모델 호출을 진행할까? 추가 지시가 필요한가? | `Proceed` 또는 `Halt` |
| `pre_tool_use` | 이번 도구를 실행해도 되는가? | `Continue` / `Deny` / `Suspend` |
| `post_tool_use` | 결과를 어떻게 기록·가공할까? | `None` |
| `before_finish` | 이 시점에 끝내도 되는가? | `Proceed` 또는 `Halt` |
| `on_resume` | 승인 입력과 현재 정책으로 다시 허용할까? | `Continue` / `Deny` / `Suspend` |
| `on_suspend` | 보류 요청에 추가로 보관할 정보가 있는가? | `None` |

`Continue`는 도구 판단에서, `Proceed`와 `Halt`는 모델 호출·종료 등 다른 경계에서 사용한다.
특히 `before_finish`의 기본 `Halt(reason)`는 기존 종료를 받아들이는 의미다. 이름만 보고 전부 오류 종료라고 해석하지 말자.

지금은 `pre_tool_use`와 `on_resume`를 이해하고 나머지는 필요할 때 돌아오면 된다.

## 코드와 테스트에서 확인하기

[controls.py](../../packages/semora/src/semora/controls.py)의 `Permissions.__call__`을 읽는다.
`asked`에 첫 보류 요청을 기억하고, `Deny`에서 바로 반환하는 부분을 찾으면 된다.

[test_controls.py](../../tests/test_controls.py)에서는 다음 두 테스트부터 본다.

- `test_a_deny_beats_a_suspend_whatever_the_order`
- `test_an_allow_does_not_short_circuit`

## 확인 질문

**Q. 앞 정책이 Continue를 반환했으니 뒤 정책은 볼 필요가 없는가?**

아니다. 뒤에서 거절하거나 승인을 요구할 수 있다.

**Q. Suspend는 “거절 결과를 모델에 전달했다”는 뜻인가?**

아니다. 외부 입력을 받기 위해 보류를 요청한 상태다.

**Q. 일곱 지점 모두 같은 세 가지 결정을 반환하는가?**

아니다. 입력 변환, 도구 허가, 종료 판단은 반환 계약이 다르다.

**다음:** [5장 — 승인을 기다리고 다시 시작하기](05-approval.ko.md)
