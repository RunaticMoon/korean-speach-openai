# Validation report

작성일: 2026-10-02

## 실행한 검증

```text
python -m compileall -q app tests examples
python -m pytest -q tests --disable-warnings

51 passed in 0.89s
```

테스트 환경:

- Python 3.13.5
- FastAPI 0.128.2 / Starlette 0.50.0
- HTTPX 0.28.1 / Pydantic 2.13.4
- python-multipart 0.0.29 / pytest 9.0.2
- FFmpeg 7.1.5 (설치된 실제 실행 파일 사용)

테스트 내용:

- Bearer 인증과 비인증 차단, 모델·음성 목록, upstream probe가 아닌 health endpoint.
- OpenAI 형태의 multipart ASR 요청, Groq 모델 alias, text/json/verbose_json/srt/vtt.
- 반복 timestamp_granularities multipart 인코딩 및 잘못된 옵션 거부.
- 빈 파일, 파일 크기 제한, 전체 HTTP body 제한.
- 한국어 TTS voice 매핑과 Google REST 요청 본문, base64 오디오 복원.
- 실제 FFmpeg를 이용한 MP3 / Ogg Opus / AAC / FLAC 인코딩, WAV / raw PCM 응답.
- 한국어 장문 UTF-8 바이트 단위 분할, 텍스트 누락 방지, WAV 헤더를 포함한 정상 병합.
- 4배속 요청을 Google 2배속 + FFmpeg 추가 속도로 처리.
- 메모리 캐시 hit 및 중복 성공 요청의 추가 상위 호출 방지.
- SQLite 예약량 유지, rolling window 만료, 10개 동시 예약의 원자적 한도 처리.
- 로컬 quota 초과 전 상위 호출 차단, upstream 429/Retry-After 전달.
- 상위 타임아웃에 재시도하지 않고 예약량 유지.
- 인증 실패 전 합성 차단, malformed upstream audio 처리, validation error에 입력 본문 미노출.

## 검증 범위의 한계

**Groq/Google 응답과 Google 인증 토큰은 모의 객체입니다.** 실제 클라우드 API 호출, 계정의 무료 플랜/무료 잔여량, Google ADC 토큰 발급·갱신, 실제 한국어 정확도·음색·지연시간은 검증하지 않았습니다.

작성 환경에는 google-auth와 OpenAI Python SDK가 설치되어 있지 않았으며 외부 패키지 저장소 네트워크 접근도 되지 않아 이 두 패키지의 설치와 실제 SDK 클라이언트 실행을 검증하지 못했습니다. google-auth는 실제 실행 시 requirements.txt로 설치하며, 모의 테스트에서는 주입한 TokenSource를 사용합니다. OpenAI SDK 예제는 공식 API의 요청 형식에 맞춰 작성했으나 실제 SDK를 실행해 확인한 것은 아닙니다.

Docker 실행 파일이 없어 이미지 build/run, requirements.txt의 인터넷 설치, Docker Compose 권한 설정, 실제 OCI ARM 환경은 실행 검증하지 않았습니다. Compose YAML은 구조를 파싱해 기본 필드와 볼륨·포트 설정을 확인했습니다.

부하 테스트, 공개 서비스 보안 감사, SDK 전체 규격 일치 검증, Realtime API 지원을 완료한 것으로 해석하면 안 됩니다. 이 구현의 범위는 README에 명시한 Audio REST 부분집합입니다.
