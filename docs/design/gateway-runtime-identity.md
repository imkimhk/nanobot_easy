# 게이트웨이 실행 상태 식별 보완 (3단계)

2026-11-16. 운영 재시작 없이 코드와 로컬 검증을 완료했다.

## 바뀐 동작

- POSIX 프로세스 그룹 ID 대신 프로세스 시작 식별값을 비교한다.
  Linux는 boot ID와 `/proc/<pid>/stat` 시작 tick, macOS는 `ps lstart`를 사용한다.
  Windows는 기존 프로세스 생성 시각을 사용한다.
- 기존 POSIX 숫자 ID, 식별값 누락, 조회 실패는 `identity_unverified`로 표시한다.
  기록을 유지하고 자동 시작·종료·재시작을 거부한다. PID가 살아 있다는 이유만으로
  nanobot 프로세스라고 판단하지 않는다. 죽은 PID나 확인된 식별값 불일치는
  기존처럼 stale 상태로 처리한다.
- 전경 실행도 시작 시 상태 파일을 생성한다. 프로세스 자신이 기록하는 정보는
  PID, 시작 식별값, 기록 시각, 포트, 설정/워크스페이스와 소스 경로다.
- `startup`에는 시작 시 Python 소스 트리의 SHA-256, 디스크 설정 파일의 SHA-256,
  메모리의 Config에서 읽은 모델/provider/context selection 값을 기록한다.
  비밀키나 설정 본문은 기록하지 않는다.
- `nanobot gateway status --config <path> --workspace <path>`는 시작 정보를
  보여준다. 식별 불가능한 기록은 `Running: unverified`로 표시한다.

## 해석 범위와 배포

시작 시 디스크 소스 지문은 로드된 모든 바이트코드의 해시가 아니다. Python 파일만
포함하며 템플릿, WebUI, 외부 의존성을 포함하지 않는다. 설정 파일 지문 또한
Config 로딩 이후의 디스크 스냅샷이다. 모델/provider는 시작 시 Config 값이며
실제 provider fallback 결과나 이후 동적 변경을 증명하지 않는다.
macOS 시작 시각 식별은 초 단위다. 게이트웨이 상태는 준비 완료/health 보증도 아니다.

별도 외부 배포 도구의 `.local/run/main-runtime.json`은 이 코드가 읽거나 갱신하지
않는다. 이 파일의 오래된 PID를 현재 실행 증거로 사용해서는 안 된다. 이번 보완은
프로젝트의 표준 `gateway.<instance>.json` 기록과 CLI 조회 경로에 적용된다.

기존 실행 프로세스에는 소급 적용하지 않았다. 다음 배포에서 새 코드로 시작해야
자체 기록이 생성된다. 이전 숫자 identity 기록으로는 자동 종료가 거부될 수 있으므로
배포 시 실제 PID와 시작 정보를 확인한 뒤 기존 supervisor로 종료해야 한다.
이 작업에서 운영 상태 파일을 삭제하거나 프로세스를 종료하지 않았다.

## 검증

- gateway runtime/service, CLI, gateway config: **36 passed**.
- 기존 개선 평가의 관련 회귀 검사: **22 passed** (실제 모델 평가 아님).
- 변경 Python 파일 Ruff 및 diff 공백 검사 통과.
- 회귀 사례: PID 재사용, 누락/구형 identity, 조회 실패 시 기록 보존과 실행 차단,
  전경 시작 기록 생성, 비밀값 제외, 시작 후 설정 파일 변경 시 기록 보존,
  부모 프로세스의 자식 시작 정보 보존, Linux process name 파싱, CLI 표시.

이 문서는 3단계 코드 검증 시점의 기록이다. 이후 배포 결과는 별도 배포 기록을 따른다.
