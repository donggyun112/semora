# 6. 중단된 작업 복구하기

[목차](README.ko.md) · [이전](05-approval.ko.md)

## 문제는 예외보다 “결과를 모름”이다

설명용으로 외부 서비스에 알림을 보내는 도구를 생각해 보자.

```text
1. Semora 저장소에 실행 시작 기록
2. 외부 서비스에 알림 요청
3. 외부 서비스가 알림 발송
4. Semora 저장소에 완료 결과 기록
```

3번과 4번 사이에 프로세스가 죽으면 어떻게 될까?
저장소에는 시작만 남았지만, 수신자는 이미 알림을 받았을 수 있다.
반대로 1번 직후에 죽었다면 알림 요청은 보내지 않았을 수 있다.

두 경우 모두 로컬 기록은 “시작했고 완료 기록은 없음”이다.
복구 코드가 기록만 보고 두 경우를 구별할 수 없다.

## 세 가지 상태

| step 상태 | 저장소가 알고 있는 사실 | 기본 복구 처리 |
|---|---|---|
| `absent` | 시작 기록이 없음 | 정책 등을 통과하면 실행 시작 |
| `running` | 시작 기록은 있고 완료 결과는 없음 | `Indeterminate`로 자동 재시도 중단 |
| `done` | 완료 결과가 기록됨 | 저장된 결과 재사용 |

`running`은 “지금도 워커가 살아서 실행 중”이라는 보장이 아니다.
`done`도 “비즈니스 작업 성공”과 같지 않다. 기록된 오류 결과도 완료된 보고이므로 `done`일 수 있다.

## 독립 실행 예제: 시작 기록과 완료 결과

아래는 저장소 상태만 관찰하는 실험이다. 실제 도구는 실행하지 않는다.
런타임 대신 저장소를 직접 조작하는 것은 이 학습 실험을 위한 것이다.

```python
import asyncio

from semora import MemorySteps


async def main():
    store = MemorySteps()
    branch = "learning-run"
    key = "tool:c1"

    print((await store.read(branch, key)).status)
    await store.start(branch, key)
    print((await store.read(branch, key)).status)
    await store.finish_effect(branch, key, {"ok": True, "value": "알림 접수됨"})
    step = await store.read(branch, key)
    print(step.status)
    print(step.value)


asyncio.run(main())
```

상태는 `absent → running → done` 순서로 출력된다.
완료 직전 프로세스가 사라진 상황은 `start` 이후 기록만 남은 상황으로 이해할 수 있다.

## 복구는 처음부터 모두 다시 하기와 다르다

두 호출이 있었고, 첫 호출만 완료되었다고 하자.

```text
tool:c1 → done, "wrote a.md"
tool:c2 → absent
```

같은 작업과 같은 호출 ID를 대상으로 복구하면 `c1`은 기록을 재사용하고 `c2`는 실행할 수 있다.
이것을 결과 replay라고 부른다. 문자열을 다시 제공하는 것이지 완료된 함수를 다시 실행하는 뜻이 아니다.

반대로 `c2`가 `running`이면 기본적으로 `Indeterminate`가 발생한다.
에러를 숨기고 계속 진행하는 대신, 호출자가 외부 상황을 확인할 수 있게 남기는 것이다.

이 동작은 [test_recovery.py](../../tests/test_recovery.py)의 다음 테스트에서 볼 수 있다.

- `test_committed_call_replays_and_absent_call_runs`
- `test_started_but_unreported_call_is_indeterminate`

## retry_running=True는 안전성을 만드는 옵션이 아니다

이 설정은 “불확실한 실행을 반복해도 괜찮다고 호출자가 판단했다”는 선택이다.
중복 알림 방지나 외부 서비스의 중복 제거를 자동으로 구현해 주지는 않는다.

재시도 판단에는 외부 작업의 성질이 필요하다. 같은 요청 키로 중복을 제거하는 서비스인지, 상태 조회로 처리 여부를 확인할 수 있는지 등이 그 예다.
이 판단은 Semora의 로컬 기록만으로 대신할 수 없다.

## 도구 호출 ID와 업무 ID는 다르다

```text
branch A의 call c1 → 고객의 주문 order-42
branch B의 call c9 → 같은 주문 order-42
```

Semora에서 두 호출은 다른 실행 기록이다.
업무적으로 같은 주문인지 판단하고 중복을 막는 키는 호스트가 관리한다.

따라서 “호출 ID로 기록하니 다른 branch에서 같은 주문을 처리해도 중복이 없다”는 결론은 나오지 않는다.
여러 실행에 걸쳐 중복을 막으려면 주문 ID 같은 안정적인 업무 식별자가 필요하다.

## 승인 재개와 완료 결과 replay도 다르다

승인 대기 중인 호출은 아직 실행해도 되는지 결정해야 한다. 그래서 `on_resume`가 필요하다.
이미 완료한 호출의 결과 replay는 과거에 일어난 일을 다시 보여 주는 것이다.
일반 복구에서 완료된 호출을 다시 승인받지는 않는다.

fork에서 복사한 결과에 새 정책을 적용하는 `regate` 옵션은 별도 기능이다. 처음에는 일반 recover와 구분만 해 두자.

## 오류 결과와 런타임 신호

일반적인 도구 예외는 오류 결과로 기록된다. 예를 들어 `OSError("disk full")`가 기록되면 다음 형태를 볼 수 있다.

```text
status = done
value = {"ok": False, "error": "disk full"}
```

함수가 오류를 반환했다는 사실이 기록된 것이다. 오류 전에 발생한 부분적인 외부 변경이 되돌려졌다는 뜻은 아니다.

`Contended`, `Fenced`, `Indeterminate`, `ControlSignal` 같은 실행 제어 신호는 보통의 도구 오류로 뭉개지 않고 전파한다.
취소 역시 실행 완료 보고로 취급하지 않는다.

## 왜 도구 결과를 먼저 기록하고 journal을 나중에 실행할까?

도구 실행 뒤 기록·가공 작업이 실패했다고 외부 도구를 다시 실행하면 안 되기 때문이다.
현재 [effects.py](../../packages/semora/src/semora/effects.py)는 원본 결과와 모델에 보여 줄 가공 결과를 구분한다.

```text
tool:c1  → 원본 실행 결과
after:c1 → post_tool_use 이후 모델에 보여 줄 결과
```

가공 결과가 기록되어 있으면 복구 때 그 결과를 재사용할 수 있다.
예를 들어 가린 정보를 복구 때 원본으로 다시 노출하지 않도록 하는 경계다.

하지만 journal이 외부 작업을 마친 직후 완료 표식을 기록하기 전에 죽으면 journal 자체는 다시 실행될 수 있다.
따라서 외부 효과가 있는 journal도 중복 실행을 고려해야 한다.
이 구분은 [test_effect_boundary.py](../../tests/test_effect_boundary.py)의 `test_recovery_replays_redacted_result_without_repeating_journal`에서 확인한다.

## 확인 질문

**Q. running이 남아 있으면 실패한 것이니 무조건 재시도해도 되는가?**

아니다. 이미 외부 효과가 발생했을 수 있다.

**Q. done이면 성공인가?**

완료 결과가 기록되었다는 뜻이다. 그 결과의 `ok`가 `False`일 수도 있다.

**Q. 저장소만 있으면 외부 작업이 정확히 한 번 수행되는가?**

아니다. 외부 효과와 로컬 기록 사이의 불확실성을 없애려면 외부 서비스의 계약까지 필요하다.

**다음:** [7장 — 저장소와 런타임 소스 읽기](07-runtime.ko.md)
