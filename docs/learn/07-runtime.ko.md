# 7. 저장소와 런타임 소스 읽기

[목차](README.ko.md) · [이전](06-recovery.ko.md)

## 이제 함수 이름보다 책임을 먼저 보자

지금까지 읽은 동작을 파일에 연결하면 다음과 같다.

| 파일·패키지 | 담당하는 질문 |
|---|---|
| `controls.py` | 정책이 무엇을 결정하고 어떻게 조합되는가? |
| `effects.py` | 실제 호출을 실행할지, 기록을 재사용할지 어떻게 처리하는가? |
| `runtime.py` | 어느 작업을 누가 실행하고, 어떻게 보류·재개·복구하는가? |
| `transcript.py` | 대화와 실행 정보를 어떻게 보관하고 복원하는가? |
| `dispatch.py` | 외부 명령을 현재 실행 상태에 어떻게 연결하는가? |
| `agent.py` | 클래스 기반 사용법을 어떻게 제공하는가? |
| `semora-store` | 저장소가 지켜야 할 인터페이스와 메모리 구현 |
| `semora-store-pg` | PostgreSQL 저장소 구현 |

## 실행 기록과 대화 기록을 구분하기

`MemorySteps`는 도구·모델 단계의 결과와 실행 제어 정보를 다루는 저장소다.
`MemoryTranscript`는 대화·실행 이력을 보관하는 저장소다.
모두 메모리 구현이라 프로세스가 사라지면 데이터가 사라진다.

“대화 기록에 도구 호출 요청이 있다”는 사실만으로 그 도구가 외부에서 실행되었는지는 알 수 없다.
반대로 완료 결과만 있어도 그 전후 대화를 이어가려면 메시지 이력이 필요하다.

승인 보류 기록에는 재개에 필요한 메시지 스냅샷도 들어간다.
따라서 별도 transcript가 없으면 모든 resume가 불가능하다고 이해하면 안 된다.
일반 복구에서는 호스트가 이력을 제공하거나, transcript에서 확정된 이력을 읽어 제공할 수 있다.

## 워커가 둘이면 어떻게 할까?

워커는 작업을 수행하는 프로세스나 실행 주체라고 생각하면 된다.
같은 branch를 두 워커가 동시에 이어가려 하면 실행 소유권을 정해야 한다.

Semora는 저장소가 있을 때 유효 기간이 있는 실행 점유인 **lease**를 사용한다.
실행 중에는 갱신하고, 시도가 끝나면 해제한다.
다른 워커가 이미 점유한 상태에서는 런타임이 `Contended`를 알린다.

여기서 문제가 하나 더 생긴다. 이전 워커가 오랫동안 멈췄다가, 새 워커가 실행권을 받은 뒤 다시 움직일 수 있다.
이때 예전 워커의 기록이 최신 상태를 덮어쓰면 안 된다.

이를 막기 위해 저장소 쓰기에 **fencing token**을 함께 제시한다.
이전 실행권의 토큰을 제시한 쓰기는 `Fenced`로 거절한다.

## 독립 실행 예제: 예전 워커의 쓰기 거절

이 실험은 메모리 저장소의 계약을 확인한다. 외부 API를 실행하지 않는다.

```python
import asyncio

from semora import MemorySteps
from semora_store import Fenced


async def main():
    store = MemorySteps()
    branch = "lease-demo"
    old_token = await store.acquire(branch, "worker-a", 60.0)
    competing = await store.acquire(branch, "worker-b", 60.0)
    print(competing == 0)  # worker-a가 점유 중이므로 True

    await store.release(branch, "worker-a")
    new_token = await store.acquire(branch, "worker-b", 60.0)
    print(new_token > old_token)  # True

    try:
        await store.start(branch, "tool:c1", old_token)
    except Fenced:
        print("예전 워커의 저장소 쓰기가 거절됨")

    await store.release(branch, "worker-b")


asyncio.run(main())
```

스토어의 `acquire`는 경쟁 시 `0`을 반환한다. 런타임의 `_lease`가 이를 `Contended`로 바꿔 호출자에게 알린다.

## fencing은 어디까지 보호할까?

토큰을 확인하는 저장소 쓰기를 보호한다.
예전 워커가 외부 알림 서비스에 보내는 요청까지 자동으로 막지는 않는다.
외부 서비스가 이 토큰을 확인하는 계약이 없다면 토큰 값만으로 외부 동작을 제어할 수 없다.

6장의 “로컬 기록과 외부 효과 사이의 틈”은 lease를 도입해도 남아 있다.
lease, fencing, 외부 중복 제거는 서로 다른 문제를 담당한다.

| 개념 | 다루는 문제 |
|---|---|
| lease | 지금 이 branch를 진행할 소유권 |
| fencing | 이전 소유자의 늦은 저장소 쓰기 |
| 업무 중복 제거 | 여러 요청이 같은 외부 업무를 반복하는 문제 |

## runtime.py에서 처음 읽을 곳

[runtime.py](../../packages/semora/src/semora/runtime.py)를 열고 다음 순서로 찾는다.

1. `AgentRuntime.run`: 실행 문맥과 lease를 준비하는 입구.
2. `_attempt`: 한 번의 시도에 쓸 이력과 `Effects`를 구성하는 부분.
3. `_park`: 승인 보류를 기록하는 부분.
4. `resume`: 답을 저장하고, 아직 답이 없는 호출을 확인하는 부분.
5. `_finalize`: 답이 모인 재개 정보를 실행 시도에 연결하는 부분.
6. `_lease`: 실행권 획득·갱신·해제를 관리하는 부분.

`run`은 `_attempt`를 호출하며, 유효한 승인 답이 모두 모이면 `resume`는 `_finalize`를 호출한다.
호출의 세부 순서보다 앞 장에서 본 책임이 어디에 나타나는지 먼저 찾는다.

`_attempt` 안에는 다음과 같은 형태의 코드가 있다. 부분 코드다.

```python
from pydantic_ai import Agent

# _attempt 내부의 핵심 형태:
# result = await Agent.run(agent, ..., capabilities=[effects, ...])
```

여기서 `Agent`는 import가 보여 주듯 Pydantic AI의 클래스다.
Semora는 `Effects`를 capability로 붙이고 Pydantic AI의 실행 기능을 사용한다.
`capability`는 이 단계에서는 “프레임워크가 제공한 실행 지점에 기능을 붙이는 객체” 정도로 이해하면 된다.

Semora의 클래스 Agent도 이 경로를 사용한다. `_attempt`에서 기본 클래스의 메서드를 명시하는 이유는 Semora의 `run` 편의 메서드로 다시 진입하는 것을 피하기 위해서다.
이 역할 분담은 [프로젝트 계약](../../AGENTS.md)과 소스 주석에 명시되어 있다.

## effects.py에서 처음 읽을 곳

[effects.py](../../packages/semora/src/semora/effects.py)의 다음 메서드를 순서대로 본다.

| 메서드 | 앞 장에서 배운 개념 |
|---|---|
| `before_tool_execute` | 도구 정책 판단, 승인 재검사, 완료 호출 구분 |
| `wrap_tool_execute` | absent/running/done에 따른 실행·재사용 |
| `_execute` | 실제 handler 호출과 성공·오류 결과 기록 |
| `_journal_once` | 모델에 보여 줄 가공 결과 저장과 재사용 |

`handler`는 Pydantic AI가 제공하는 다음 실행 동작이다. 별도의 도구 탐색기나 두 번째 에이전트 루프를 Semora에서 새로 만드는 것이 아니다.

`wrap_tool_execute`는 새 실행이 필요한 분기에서 `_execute`를 사용한다.
`_execute`의 완료 기록 이후 `_journal_once`를 거치는 순서를 6장의 실패 상황과 연결해 보면 된다.

현재 구현에는 `wrap_model_request`도 있다. 저장소가 있으면 모델 응답도 단계로 기록하고, 같은 단계의 완료 응답을 재사용한다.
“Semora는 도구 결과만 기록한다”로 범위를 좁혀 외우지 말자.
결과가 불명확한 모델 단계도 기본적으로 `Indeterminate`로 남을 수 있다. 모델 호출의 외부 과금까지 무조건 한 번이라는 보장은 아니다.

## dispatch는 새로운 모델 루프인가?

아니다. 외부에서 들어오는 명령을 기존 런타임 동작에 연결하는 라우팅 계층이다.

| 명령 | 의도 |
|---|---|
| `Prompt(text, prompt_id=...)` | 사용자 입력 전달 |
| `Answer(pending_id, payload)` | 특정 승인 요청에 답변 |
| `Recover()` | 중단된 작업 이어가기 |

`Answer`가 왔다고 항상 재개할 수 있는 것은 아니다. 기다리는 승인이 없으면 부적절한 상태 전이일 수 있다.
실행 중 들어온 `Prompt`는 기본 라우터에서 큐에 넣는 경로로 이어질 수 있다.

여러 번 전송될 수 있는 동일 입력에는 안정적인 `prompt_id`가 필요하다.
이것은 도구 호출 ID나 주문 ID와도 다른, 입력 전달의 식별자다.

[dispatch.py](../../packages/semora/src/semora/dispatch.py)는 `default_router()`와 각 전이 클래스의 `states`, `applies`, `apply` 순서로 읽는다.
[test_dispatch.py](../../tests/test_dispatch.py)의 `test_an_answer_with_no_park_is_an_invalid_transition`과 `test_a_repeated_prompt_id_is_delivered_once`가 좋은 출발점이다.

## 저장소 구현은 마지막에 내려가자

[semora_store/ledger.py](../../packages/semora-store/src/semora_store/ledger.py)에서 먼저 `ExecutionStore` Protocol을 본다.
Protocol은 저장소 구현이 제공해야 할 동작을 선언한다.
그 아래 `MemorySteps`에서 그 동작을 메모리 자료구조로 어떻게 구현하는지 확인한다.

`semora-store`는 Pydantic AI를 몰라도 되는 저장소 계약이다.
PostgreSQL 어댑터는 별도 패키지다. 학습 초기에는 SQL 구현보다 `start`, `finish_effect`, `acquire`의 의미가 먼저다.

PostgreSQL의 내구성 검증은 별도의 환경이 필요하다.
`SEMORA_TEST_DSN` 없이 건너뛴 PostgreSQL 테스트나 메모리 테스트 통과를 실제 DB 복구 보장으로 해석하지 않는다.
관련 테스트는 [test_store_conformance.py](../../tests/test_store_conformance.py)에 있다.

## 혼자 소스를 읽는 작은 루틴

“이 파일 전체가 무슨 뜻이지?” 대신 질문 하나를 잡는다.

예를 들어 “이미 완료한 도구가 복구 때 또 실행되는가?”를 선택한다.

1. [test_recovery.py](../../tests/test_recovery.py)의 `test_committed_call_replays_and_absent_call_runs`를 연다.
2. 테스트 마지막 `assert files.ran == ["b.md"]`를 먼저 읽는다.
3. 위로 올라가 `c1`만 완료 상태로 준비한 코드를 본다.
4. `Effects.wrap_tool_execute`에서 `done`일 때 새 실행 분기를 지나지 않는 것을 찾는다.

이렇게 **기대 결과 → 준비한 상태 → 실제 분기** 순서로 읽으면 함수 전체를 암기할 필요가 없다.

자료에서 다룬 주요 동작은 아래 명령으로 확인할 수 있다.

```bash
uv run pytest -q tests/test_controls.py tests/test_hitl.py tests/test_recovery.py tests/test_effect_boundary.py tests/test_dispatch.py
```

## 확인 질문

**Q. lease가 있으면 외부 알림의 중복까지 자동으로 막을 수 있는가?**

아니다. 실행 소유권과 외부 업무 중복 제거는 별개다.

**Q. `interrupted`라는 상태만 보고 워커가 죽었다고 판단해도 되는가?**

아니다. 아직 실행 중인 워커도 포함할 수 있다. 실제 점유 여부는 lease 획득 과정으로 구분한다.

**Q. Semora가 Pydantic AI 대신 모델의 다음 행동을 계획하는가?**

아니다. 모델과 도구의 루프는 Pydantic AI가 담당한다. Semora는 그 실행의 정책·기록·생명주기를 담당한다.

**Q. 다음에는 무엇을 더 읽을까?**

승인·복구의 핵심을 설명할 수 있게 되었다면 [test_resume_concurrency.py](../../tests/test_resume_concurrency.py), [test_fork.py](../../tests/test_fork.py), [test_inputs.py](../../tests/test_inputs.py)에서 관심 있는 시나리오 하나를 고른다.
동시 승인, 실행 분기, 입력 큐는 앞 장의 개념을 확장하는 주제다.

**다시 보기:** [학습 목차](README.ko.md)
