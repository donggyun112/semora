# 1. 도구 인자와 애플리케이션 데이터

[목차](README.ko.md) · [이전: 기초](../PYDANTIC-AI-BASICS.ko.md)

## 출발점: Application에서 사용자 정보를 꺼내면 되지 않을까?

그렇게 해도 된다. 이미 요청별 `Application` 객체가 있고, 도구가 그 객체를 참조하도록 구성했다면 필요한 데이터가 도구에 도달한다.

예를 들어 다음은 구조를 보여 주는 부분 코드다.

```python
class UserTools:
    def __init__(self, application):
        self.application = application

    def current_user(self) -> str:
        return self.application.request.user.name
```

이 인스턴스의 `current_user` 메서드를 도구로 등록하면 사용자 이름을 읽을 수 있다.
클로저로 요청 객체를 잡아 두는 방법도 가능하다.

이런 구조가 이미 잘 작동한다면 `deps` 때문에 반드시 바꿀 이유는 없다.

## deps가 제공하는 것은 전달 규칙이다

`deps`는 이번 실행에서 사용할 객체를 `run()`에 전달하고, 도구가 `ctx.deps`로 읽게 하는 Pydantic AI의 공통 인터페이스다.
클래스 이름은 `Deps`일 필요가 없다. `RequestData`, `Services`, 기존 `Application` 타입도 목적에 맞게 사용할 수 있다.

다음 부분 코드에서 오른쪽 객체를 왼쪽 인자에 전달한다.

```python
request_data = Deps(user_name="민수")
# 비동기 함수 안에서:
# result = await agent.run("내 이름은?", deps=request_data)
```

`deps_type=Deps`는 타입을 선언한다. 객체를 생성하거나 DB 연결을 열어 주는 설정이 아니다.
실제 객체를 만들고 필요한 자원을 준비하는 것은 애플리케이션의 책임이다.

이 전달 방식은 [Pydantic AI Dependencies 문서](https://pydantic.dev/docs/ai/core-concepts/dependencies/)에서 설명한다.

## LLM이 채우는 인자와 서버가 제공하는 객체

다음은 도구 등록 부분 코드다.

```python
@agent.tool
def greet(ctx: RunContext[Deps], greeting: str) -> str:
    return f"{greeting}, {ctx.deps.user_name}님!"
```

모델이 채우는 인자는 `greeting`이다. `ctx`는 프레임워크가 전달하며 모델용 인자 스키마에서 빠진다.
`ctx`만 받는 도구라면 모델 입장에서는 인자 없는 도구다.
이 구분은 [공식 Tool 문서](https://pydantic.dev/docs/ai/tools-toolsets/tools/)의 데코레이터와 스키마 설명에 해당한다.

```text
모델의 요청: greet({"greeting": "안녕하세요"})
앱의 데이터: user_name = "민수"
실행 결과:  "안녕하세요, 민수님!"
```

이름을 프롬프트에 넣어서 모델이 도구 인자로 다시 적게 만드는 과정은 필요 없다.
다만 도구가 사용자 정보를 반환하면 그 반환값은 모델에 전달된다. `deps`는 비밀 정보를 자동으로 가리는 장치가 아니다.

## 두 방식은 무엇이 다른가?

| 비교 | Application에 묶인 도구 | ctx.deps를 사용하는 도구 |
|---|---|---|
| 데이터 접근 | `self.application.request.user` | `ctx.deps.user` |
| 연결 시점 | 요청 객체를 도구 인스턴스 등에 연결할 때 | `run(deps=...)` 호출 시 |
| 도구가 아는 구조 | Application과 request의 구조 | 선언한 의존성 타입의 구조 |
| 테스트 | 가짜 Application을 연결 | 가짜 의존성 객체를 전달 |
| 객체 수명 관리 | 애플리케이션이 설계 | 애플리케이션이 설계 |

둘 다 올바르게 구성할 수 있다. 이 자료에서 `deps`를 배우는 이유는 우월해서가 아니라, Pydantic AI 코드에서 자주 만나는 인터페이스이기 때문이다.

요청마다 Application 인스턴스를 만드는 방식이라면 사용자별 데이터를 분리할 수 있다.
반대로 공유 객체 하나의 `request`를 계속 바꾸는 방식은 동시에 실행되는 요청을 어떻게 구분할지 따로 설계해야 한다.
`deps`도 같은 가변 객체를 여러 실행에 넘기면 객체 자체가 자동으로 복제되지는 않는다.

## 도구 내부의 권한 검사도 계속 의미가 있다

서버가 제공한 사용자 정보로 도구나 서비스가 권한을 검사하는 것은 자연스러운 설계다.
Semora를 붙인다고 비즈니스 서비스의 접근 제어가 필요 없어지는 것은 아니다.

이후 배울 Semora의 정책은 “이번 에이전트 실행에서 이 도구를 진행할지”를 결정한다.
서비스의 “이 사용자가 이 문서를 수정할 수 있는지” 검사와 함께 사용할 수 있다.

## 확인 질문

**Q. 이미 요청별 Application을 도구에 전달하고 있다. deps로 옮겨야 할까?**

반드시 그럴 필요는 없다. 의존성을 전달하는 경로와 도구가 결합하는 인터페이스를 선택하는 문제다.

**Q. `ctx`와 `path`를 받는 도구에서 모델은 무엇을 채울까?**

`path`를 채운다. `ctx`와 그 안의 `deps`는 프레임워크와 애플리케이션이 제공한다.

**Q. deps를 쓰면 로그인 사용자가 자동으로 찾아지는가?**

아니다. 애플리케이션이 인증된 사용자를 확인하고 해당 객체를 전달해야 한다.

**다음:** [2장 — 모델과 도구가 대화하는 과정](02-agent-loop.ko.md)
