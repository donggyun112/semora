# 3. Semora 예제 한 개 읽기

[목차](README.ko.md) · [이전](02-agent-loop.ko.md)

## 이번 장의 질문

> 모델이 읽기·검색·쓰기를 모두 요청했다. 왜 쓰기만 실행을 보류할까?

[examples/permissions.py](../../examples/permissions.py)를 대상으로 읽는다.
실제 파일 작업은 하지 않고 `touched` 목록에 실행된 작업을 남기는 예제다.

```bash
uv run python examples/permissions.py
```

## 파일의 시작보다 demo()부터 읽자

`demo()` 안의 핵심 부분 코드다.

```python
agent = Worker(branch_id="perm-1")
# 비동기 demo() 안에서:
# parked = await agent.run("look around, then fix it")
# outcome = await agent.resume({"type": "approve"})
```

`Worker`는 여기서 `semora.Agent`의 하위 클래스다.
이 클래스 인터페이스의 인스턴스는 한 branch에 연결된다.
`branch_id`는 시작·승인·복구가 같은 작업임을 찾는 좌표라고 생각하면 된다.

Pydantic AI 기본 Agent를 여러 요청에 재사용하는 것과 Semora의 이 편의 인터페이스를 혼동하지 말자.
자세한 계약은 [API — Optional class agent](../API.md#optional-class-agent)에 있다.

## 첫 번째로 볼 것: 실제 도구

읽기 도구는 다음처럼 선언되어 있다. 부분 코드다.

```python
@tool(metadata={"permission": "read"})
async def read(self, path: str) -> str:
    """Read a file."""
    self.touched.append(f"read {path}")
    return f"contents of {path}"
```

여기서 모델이 채우는 작업 인자는 `path`다. `self`는 등록된 인스턴스 메서드의 객체다.
`metadata`는 개발자가 도구에 붙인 선언이다.

모델이 “이 호출은 read 권한이야”라고 주장해서 권한 등급이 정해지는 구조가 아니다.
권한 분류는 호스트 애플리케이션이 등록한 도구 정의에서 가져온다.

현재 예제의 도구 분류는 다음과 같다.

| 도구 | 개발자가 선언한 permission | 도구 본문의 모의 동작 |
|---|---|---|
| `read` | `read` | 읽기 기록 추가 |
| `grep` | `read` | 검색 기록 추가 |
| `write` | `write` | 쓰기 기록 추가 |

## 두 번째: 정책이 붙는 자리

`Worker` 클래스에는 다음 설정이 있다. 부분 코드다.

```python
pre_tool_use = Permissions(allow({"permission": "read"}))
```

안쪽부터 읽는다.

1. `{"permission": "read"}`는 선택할 도구의 메타데이터 조건이다.
2. `allow(...)`는 그 조건을 검사하는 비동기 정책 함수 `stage`를 만들어 반환한다.
3. `Permissions(...)`는 정책 함수들을 조합한다. 여기서는 하나만 넣었다.
4. `pre_tool_use`에 연결해서 도구 실행 전에 판단한다.

`allow()`가 곧바로 도구를 허용하는 것이 아니라 **나중에 검사할 함수를 만든다**는 점이 핵심이다.

## 세 번째: stage()가 보는 두 객체

| 객체 | 담긴 정보 |
|---|---|
| `call` | 이번 호출의 이름, 인자, 호출 ID |
| `ctx.tool` | 등록된 도구 정의와 메타데이터 |
| `ctx.run` | Pydantic AI의 실행 문맥 |

현재 예제는 `matches_tool_selector(selector, ctx.run, ctx.tool)`로 조건을 검사한다.
Semora에서 선택 규칙을 새로 구현하지 않고 Pydantic AI의 선택 함수를 사용한다.
처음에는 이 함수가 “등록된 도구가 선택 조건에 맞는지 검사한다”는 것만 이해하면 된다.

조건에 맞으면 `Continue()`, 맞지 않으면 `Suspend(...)`를 반환한다.
이 예제는 **read만 즉시 허용하고 나머지는 승인 대기시키는 정책**이다. Semora 전체의 기본 정책이 아니다.

## 네 번째: 모델이 요청한 세 호출

`scripted()`는 처음에 다음 순서로 요청을 만든다.

```text
c1: read(path="a.py")
c2: grep(pattern="TODO")
c3: write(path="a.py", text="x")
```

`FunctionModel(scripted)`이므로 여기서는 이 요청을 사람이 코드로 정했다.

실행 결과를 예제의 `assert`로 읽으면 된다.

```text
run 이후:
  touched = ["read a.py", "grep TODO"]
  pending = (("approve-c3", "c3"),)

승인 후:
  touched = ["read a.py", "grep TODO", "write a.py"]
```

`approve-c3`는 외부에서 답할 승인 요청 ID, `c3`는 모델의 도구 호출 ID다.
서로 연결되지만 역할이 다르다.

## 도구 본문에서 if로 검사해도 되지 않을까?

가능하다. 읽기나 쓰기 함수에서 권한을 검사하는 구현도 유효하다.

이 예제의 구성은 여러 도구에 같은 분류·정책을 적용하는 위치를 모아 둔다.
정책이 “사용자 응답을 기다려야 한다”고 결정했을 때 실행 상태를 보관하고 나중에 재개하는 부분까지 Semora의 경로에 연결된다.

단순 권한 검사 한 번만 필요하다면 직접 구현도 충분하다.
비교할 것은 `if`를 쓸 수 있는지가 아니라, 승인과 복구까지 포함한 실행 규칙을 어디서 관리할지다.

## 확인 질문

**Q. write라는 함수 이름 자체가 자동으로 위험한 도구라는 뜻인가?**

아니다. 이 예제에서는 도구에 붙인 `permission` 값과 정책이 판단 기준이다.

**Q. 모델이 호출 인자에 `permission="read"`를 넣으면 분류가 바뀌는가?**

아니다. 정책이 보는 분류는 호출 인자 대신 등록된 도구 정의에 있다.

**Q. 이 예제로 실제 프로세스 재시작 뒤 복구까지 증명되는가?**

아니다. `MemorySteps`와 `touched`는 메모리에 있다. 여기서는 제어 흐름을 관찰한다.

**다음:** [4장 — 정책을 표현하고 조합하기](04-controls.ko.md)
