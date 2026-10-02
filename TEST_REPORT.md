# 검증 보고서

검증일: 2026-10-02 (KST)

사용자가 제공한 ZIP 원본은 Git 이력의 `Import user-provided Korean speech gateway baseline` 커밋에 보존했습니다. 원본 테스트는 별도 디렉터리에서 실행해 **51개 통과**를 확인했습니다. 아래 결과는 보완한 현재 구현에 대한 별도 검증입니다.

## 로컬 검증

```text
python -m pytest -q
135 passed  # 임시 파일 취소 경합 테스트 2개 추가 전 전체 실행

python -m pytest -q tests/test_audio.py
23 passed  # 취소 경합 테스트 2개를 포함한 최종 오디오 검증

python -m ruff check .
All checks passed!

python -m ruff format --check .
python -m pip check
python -m compileall -q speech_proxy app tests examples
bash -n scripts/run-local.sh
git diff --check
```

테스트 환경은 Linux ARM64, Python 3.12.3입니다. 의존성은 `requirements.txt`와 `requirements-dev.txt`에 고정했습니다. FastAPI 0.142.2, HTTPX 0.28.1, google-auth 2.59.1, OpenAI Python SDK 2.54.0을 실제 설치해 실행했습니다.

최종 테스트는 총 137개입니다. 최종 로컬 전체 재실행 중 호스트 메모리·스왑 포화와 높은 I/O 대기가 발생해 일부 FFmpeg/ffprobe 검사가 10초 제한을 초과했습니다. 나머지 126개는 통과했고 실패 범위를 포함한 오디오 테스트 23개는 별도 재실행에서 모두 통과했습니다. 시간 제한을 늘리지 않고 [GitHub Actions의 전체 테스트](https://github.com/RunaticMoon/korean-speach-openai/actions/runs/37012066766)에서도 통과를 확인했습니다.

검증한 동작:

- OpenAI SDK의 실제 요청 직렬화, multipart 전사 응답 파싱, binary streaming reader의 파일 저장. 공급자 HTTP 응답은 `httpx.MockTransport`, 브리지 HTTP는 ASGI 전송으로 연결했습니다.
- 실제 Uvicorn 프로세스를 loopback 포트에 실행해 `/health`·`/ready`·인증 차단과 동기 OpenAI SDK 모델 조회를 확인하고 종료했습니다.
- Paseo와 동일한 모델 별칭·한국어 전사와 헤더 없는 24kHz mono signed 16-bit LE PCM 응답. Paseo 공식 문서와 공개 소스의 요청/재생 계약을 대조했습니다.
- 실제 FFmpeg/ffprobe로 WAV·MP3·Ogg Opus·AAC·FLAC·raw PCM 처리, 한국어 UTF-8 분할, WAV 헤더 검증과 PCM 결합, 4배속, duration 없는 WebM·뒤쪽 메타데이터 M4A의 길이 측정.
- 요청/출력 크기·동시성·전체 처리 시간 제한, 잘못된 오디오 거부, subprocess 취소·시간 초과 정리, 인증 전 body 파싱 방지.
- Google ADC 탐색·파일 경로·동시 토큰 갱신 흐름과 quota project 헤더. Google 자격 증명 객체와 토큰은 모의 객체입니다.
- Google 인증 실패 시 사용량 미차감, 호출 전 전체 텍스트 예약, 실패·시간 초과 시 예약 유지, 같은 합성의 동시 요청·출력 형식 간 PCM 캐시 재사용.
- SQLite 예약 경쟁·재시작·기간 만료·한도 초과·잠금 오류, 원본 `reservations`의 한 번만 수행되는 동시 시작 마이그레이션, Groq 요청/음성 초 제한.
- 원본 환경변수 이름·음성 별칭·`google-wavenet`·자동 언어 감지·4배속 유지, 공개 예제 인증키 거부, 입력 내용과 공급자 비밀을 오류에 노출하지 않는 처리.

## 배포 검증

Compose YAML과 Paseo JSON 예제를 파싱하고 설정을 검토했습니다. 이 개발 환경에는 Docker가 없어 로컬 Docker build/run을 실행하지 않았습니다. GitHub Actions에서 의존성 설치, 정적 검사, 전체 테스트와 Docker 빌드가 통과했습니다. 첫 시작 검사는 Uvicorn 준비 전 Docker 포트 접속의 connection reset으로 실패하여, 한정된 시간 동안 준비를 기다리도록 수정했습니다. 비특권·읽기 전용 컨테이너의 `/health` 검사까지 포함한 최종 결과는 저장소의 [Actions](https://github.com/RunaticMoon/korean-speach-openai/actions)에서 확인할 수 있습니다.

## 미검증 범위

실제 Groq·Google 유료/무료 API 호출, 실제 ADC 토큰 발급·갱신, 계정 무료 잔여량, 한국어 정확도·음색·지연시간, Paseo 마이크부터 스피커까지의 연결은 검증하지 않았습니다. 실제 키·ADC를 설정한 뒤 README의 짧은 샘플 호출로 확인해야 합니다. 사용자 Paseo daemon의 설정이나 실행 상태는 변경하지 않았습니다.

이 결과는 공개 서비스 부하·보안 감사, 공급자의 무과금 보장, OpenAI 전체 API·Realtime 호환 검증을 의미하지 않습니다.
