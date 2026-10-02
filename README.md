# Korean Speech → OpenAI-compatible gateway

Groq Free-plan ASR + Google Cloud WaveNet TTS를 OpenAI Audio REST 형태로 제공하는 개인용 중계 서버입니다.

```text
음성 기능을 지원하는 앱 / OpenAI SDK
               ↓ Bearer PROXY_API_KEY
          이 서버의 /v1
               ├── /audio/transcriptions → Groq whisper-large-v3-turbo
               └── /audio/speech         → Google ko-KR-Wavenet-A/B/C/D
```

**이 서버는 OpenAI 서비스를 호출하지 않습니다.** `whisper-1`, `tts-1`, `alloy` 등은 로컬 호환 별칭입니다. 실제 OpenAI 모델·목소리를 재현하지 않습니다. 일반 LLM의 `/chat/completions`나 `/responses`를 중계하는 서버도 아닙니다.

## 1. 무료 사용의 정확한 의미

2026-10-02 공식 문서 확인 기준입니다. 요금·정책은 변경될 수 있으므로 실제 계정 콘솔을 확인하세요.

- Groq는 **Free 플랜 조직의 API 키**를 사용하세요. 공개 Whisper 무료 한도는 20 RPM, 2,000 RPD, 시간당 음성 7,200초, 하루 28,800초입니다. 실제 조직 한도가 우선입니다. 이 서버는 API 키만으로 플랜을 검증하거나 Developer 플랜을 Free로 바꾸지 못합니다.
- Google Cloud TTS는 **결제 계정 활성화가 필요**합니다. WaveNet 공개 무료 구간은 월 400만 자이고 초과분은 유료입니다. Standard와 WaveNet은 공개 가격표의 SKU가 같으므로 둘을 사용한다고 무료량이 각각 추가된다고 가정하지 마세요. Gemini / AI Studio API 키를 넣는 구성이 아닙니다.
- Google 사용량을 로컬 SQLite에 예약 기록합니다. 기본값은 **최근 32일 350만 단위 / 최근 24시간 15만 단위**입니다. 달 경계와 타임존의 차이를 보수적으로 처리하려고 달력 월 대신 rolling window를 사용합니다. 일반 한글은 1자=1단위, 일부 보조 Unicode 문자는 보수적으로 2단위로 셉니다.
- 한도를 넘으면 **상위 API를 호출하기 전에 429로 차단**합니다. 유료 모델·다른 제공자로 자동 전환하지 않습니다. 요청 재전송도 하지 않습니다. 다만 Google 인증 토큰 취득 과정에는 인증 라이브러리 자체 처리가 있을 수 있습니다.
- 타임아웃·부분 합성 실패도 예약량을 환급하지 않습니다. 실패했다고 해서 제공자가 처리하거나 과금하지 않았다고 확정할 수 없기 때문입니다. `/usage`는 실제 청구량이 아니라 **보수적 로컬 예약량**입니다.
- **완전한 무과금 보증은 아닙니다.** 다른 앱/다른 인스턴스/같은 과금 범위의 다른 프로젝트 사용량, 기존에 쓴 무료량, 과금 정책 변경을 알 수 없습니다. 처음 시작할 때 이미 무료량을 사용했다면 그만큼 더 낮게 설정하세요. 전용 프로젝트·자격 증명으로 이 서버에 사용을 모으고 Cloud Billing도 확인하세요.
- `speech-data` Docker volume이나 `data/usage.sqlite3`를 삭제하면 로컬 사용량 기록을 잃습니다. `docker compose down -v`를 실행하지 마세요. 독립 DB로 복제한 여러 서버를 함께 사용하지 마세요. 단일 호스트의 같은 DB를 공유한 프로세스에서는 예약을 원자적으로 처리하지만, 이 배포는 1개 인스턴스를 기준으로 합니다.
- Google의 **알림 전용 budget은 사용 중단 장치가 아닙니다.** 지원 서비스에 대한 spend-cap 기능 존재 여부와 적용 범위는 계정 콘솔에서 별도로 확인하세요.

공식 근거:

- Groq ASR: https://console.groq.com/docs/speech-to-text
- Groq 한도: https://console.groq.com/docs/rate-limits
- Google 가격·결제: https://cloud.google.com/text-to-speech/pricing
- Google 예산: https://docs.cloud.google.com/billing/docs/how-to/budgets

## 2. 구현 범위

| 기능 | 이 서버의 동작 |
|---|---|
| `POST /v1/audio/transcriptions` | OpenAI 형태의 multipart upload → Groq |
| ASR model | `whisper-1`, `whisper-large-v3-turbo` 모두 Groq Turbo로 매핑 |
| ASR language | 기본 `ko`; `en` 등 2자리 코드 지원; `auto`는 이 서버의 언어 자동 감지 확장값 |
| ASR 형식 | `json`, `text`, `verbose_json`; `srt`, `vtt`는 실제 segment 타임스탬프를 변환 |
| ASR timestamp | `timestamp_granularities[]`의 `word`, `segment`; `verbose_json`에서만 |
| ASR prompt, temperature | Groq에 전달. prompt는 upstream의 224토큰 제한이 적용됨 |
| ASR 파일 | 최대 25,000,000바이트; flac/mp3/mp4/mpeg/mpga/m4a/ogg/wav/webm |
| `POST /v1/audio/speech` | OpenAI 형태의 JSON → Google 합성 → 오디오 바이너리 |
| TTS model | `tts-1`, `google-wavenet` 모두 Google 한국어 WaveNet |
| TTS voice | OpenAI식 별칭 또는 `ko-KR-Wavenet-A/B/C/D` 직접 지정 |
| TTS 출력 | mp3, opus(Ogg container), aac(ADTS), flac, wav, pcm |
| PCM | 24kHz, mono, signed 16-bit little-endian, 헤더 없음 |
| TTS speed | 0.25–2.0은 Google speakingRate; 2.0 초과–4.0은 추가 FFmpeg atempo 적용 |
| 한국어 장문 | 최대 4,096자; 4,500 UTF-8바이트 이하로 분할하고 PCM을 병합 후 1개 파일로 인코딩 |
| `GET /v1/models` | 사용 가능한 로컬 호환 model ID 목록 |
| `GET /v1/voices` | 음성 매핑 목록. 이 서버 자체 확장 엔드포인트 |
| `GET /usage` | Bearer 인증이 필요한 로컬 예약 사용량 |
| `GET /health` | 프로세스 응답 확인만. 실제 upstream 인증/통신 성공 여부는 검사하지 않음 |

**지원하지 않음:** Realtime/WebSocket/WebRTC, ASR 실시간 부분 전사·화자 분리, 음성 번역 API, 자연어 `instructions`, SSE, OpenAI 음성 복제, Chat Completions/Responses. 지원하지 않는 옵션은 조용히 무시하지 않고 400을 반환합니다. `tts-1-hd`, GPT 계열 ASR/TTS model ID도 거부합니다.

`with_streaming_response`처럼 바이너리 HTTP 응답을 스트림으로 읽는 클라이언트와 사용할 수 있는 응답 형태이지만, **전체 합성이 끝난 후 응답**합니다. 음성을 생성하면서 즉시 첫 오디오 청크를 내보내는 실시간 합성은 아닙니다. 긴 입력은 합성/인코딩 시간이 길어지고 분할 경계에서 억양이 끊길 수 있습니다.

이 구현은 개인 음성 명령/짧은 답변 낭독용입니다. 공개 다중 사용자 SaaS, 대규모 업로드, 길고 느린 장문 낭독의 부하 테스트는 하지 않았습니다.

API 규격 근거:

- OpenAI speech: https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create
- OpenAI transcription: https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create
- Google 합성: https://docs.cloud.google.com/text-to-speech/docs/reference/rest/v1/text/synthesize
- Google 5,000바이트 제한: https://docs.cloud.google.com/text-to-speech/quotas
- Google 속도/포맷: https://docs.cloud.google.com/text-to-speech/docs/reference/rest/v1/AudioConfig
- 한국어 음성: https://docs.cloud.google.com/text-to-speech/docs/list-voices-and-types

## 3. 준비

Groq API 키와 Google 자격 증명은 **서버에만** 두고, 클라이언트에는 이 서버의 `PROXY_API_KEY`만 넣습니다. 키나 Google JSON 내용을 채팅에 올리지 마세요.

```bash
cd korean-speech-openai
cp .env.example .env
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

출력된 난수를 `.env`의 `PROXY_API_KEY`에 넣고, `GROQ_API_KEY`, `GOOGLE_CLOUD_PROJECT`를 채우세요. 기본값은 `ko-KR-Wavenet-A`, `127.0.0.1:8787`입니다.

### Google 자격 증명: 로컬 시험용 ADC

Google Cloud 프로젝트를 만들거나 본인이 관리하는 프로젝트를 선택하고, Cloud Billing 연결과 Cloud Text-to-Speech API 활성화를 먼저 완료하세요. Google Cloud CLI가 설치된 로컬 Mac/Linux에서 아래를 실행합니다. **해당 프로젝트 설정을 변경할 권한이 필요**하며 CLI 인증과 앱용 ADC 인증은 별개입니다.

```bash
export PROJECT_ID='실제-google-cloud-project-id'
gcloud auth login
gcloud config set project "$PROJECT_ID"
gcloud services enable texttospeech.googleapis.com --project="$PROJECT_ID"
gcloud auth application-default login
gcloud auth application-default set-quota-project "$PROJECT_ID"

mkdir -p secrets
chmod 700 secrets
cp "$(gcloud info --format='value(config.paths.global_config_dir)')/application_default_credentials.json" \
  secrets/google-adc.json
chmod 600 secrets/google-adc.json
```

`set-quota-project`에서 권한 오류가 발생하면 해당 ADC 사용자에게 quota project의 `serviceusage.services.use` 권한이 있는지 관리자에게 확인하세요. 개인 프로젝트라면 프로젝트 소유자 계정으로 시작하고, 상시 운영 권한은 최소화하세요.

이 파일에는 refresh token 등 민감한 자격 증명이 들어갑니다. 위 로그인 방식은 **로컬 개발/시험용**입니다. OCI에 상시 운영할 때는 개인 사용자 ADC를 계속 복제하기보다 전용 서비스 계정 또는 Workload Identity Federation으로 분리하세요. 앱 코드는 Google ADC를 사용하므로 서비스 계정 키 JSON이나 ADC용 외부 계정 구성도 읽을 수 있습니다. 다만 WIF 구성에서 참조하는 외부 토큰 파일/실행 명령은 별도로 컨테이너에 제공해야 합니다. 서비스 계정 키를 사용할 경우 최소 권한·보관·주기적 교체가 필요하며, 조직에서 키 발급을 금지했다면 정책을 우회하지 마세요.

Google 공식 인증 문서:

- https://docs.cloud.google.com/docs/authentication/provide-credentials-adc
- https://docs.cloud.google.com/text-to-speech/docs/authentication

## 4. 실행: Docker Compose

Docker Engine와 `docker compose`가 설치된 Ubuntu 또는 Docker Desktop이 있는 Mac을 전제로 합니다. Dockerfile은 특정 CPU 아키텍처를 강제하지 않지만, **실제 OCI ARM 이미지 빌드는 이 패키지 작성 환경에서 검증하지 못했습니다**.

컨테이너는 root가 아닌 UID 10001로 실행합니다. 자격 증명은 **파일 하나만** read-only bind mount 합니다. 간단한 개인 환경에서는 호스트의 `secrets` 디렉터리를 소유자만 접근 가능하게 두고, 그 안의 파일은 컨테이너 사용자도 읽을 수 있게 설정합니다.

```bash
chmod 700 secrets
chmod 444 secrets/google-adc.json
# 파일 읽기 권한을 넓히는 대신, 부모 secrets 디렉터리의 700 보호를 반드시 유지하세요.
# 디렉터리를 공유하거나 공개 저장소/동기화 폴더에 넣지 마세요.

docker compose up -d --build
docker compose logs --tail=50 speech-api
curl -f http://127.0.0.1:8787/health
```

보안 정책상 `0444` 파일 모드를 허용하지 않으면 UID 10001 소유의 `0400` 파일이나 접근이 제한된 호스트 ACL을 사용하세요. 단순히 `chmod 600`을 적용한 본인 소유 파일은 컨테이너 UID 10001이 읽지 못할 수 있습니다.

데이터는 named volume `speech-data`에 유지됩니다. 업데이트는 같은 프로젝트 디렉터리에서 `docker compose up -d --build`로 실행하세요. `down -v`로 volume을 삭제하지 마세요. `.env` 변경은 컨테이너 재생성이 필요합니다.

읽기 전용 root filesystem, root 권한 제거, 메모리/프로세스 제한을 사용합니다. `/tmp`는 메모리 기반이며 multipart 임시 파일이 여기로 갈 수 있습니다. TTS 캐시도 프로세스 메모리에만 저장합니다. SQLite에는 시간과 예약 문자 단위 수만 남습니다.

### OCI + Tailscale

해당 호스트의 Tailscale IPv4를 확인해서 `.env`의 `BIND_IP`에 직접 넣습니다. 이전에 사용하던 주소를 그대로 가정하지 마세요.

```bash
tailscale ip -4
# .env를 편집: BIND_IP=실제-100.x-주소
docker compose up -d
```

다른 Tailnet 기기의 음성용 base URL은 `http://100.x.x.x:8787/v1`입니다. 서버의 Tailscale 주소가 먼저 준비되어 있어야 Docker가 해당 주소에 bind할 수 있습니다. 외부 공개 IP의 8787 포트는 열지 마세요. 공용 인터넷을 사용해야 하는 구성은 TLS·접근 제어·요청 제한을 갖춘 별도 reverse proxy를 구성해야 하며 이 패키지에는 포함하지 않았습니다.

## 5. Docker 없이 Mac / Ubuntu에서 실행

Python 3.12 이상과 FFmpeg를 설치한 뒤:

```bash
# Ubuntu 예: sudo apt-get update && sudo apt-get install -y python3-venv ffmpeg
# Mac 예: brew install python ffmpeg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod 600 secrets/google-adc.json
./scripts/run-local.sh
```

`run-local.sh`는 본인이 작성한 `.env`를 shell로 읽습니다. 신뢰할 수 없는 사람이 만든 `.env`를 사용하지 마세요. 로컬 실행의 DATA_DIR은 `./data`, Docker 실행의 DATA_DIR은 named volume으로 고정됩니다. 둘을 동시에 사용하면 별도 원장이 되므로 무료량을 공동으로 관리하지 못합니다.

## 6. 앱 연결값

```text
음성 ASR/TTS Base URL: http://127.0.0.1:8787/v1
음성 API Key:         .env의 PROXY_API_KEY
ASR Model:            whisper-1
TTS Model:            tts-1
TTS Voice:            alloy
Language:             ko
```

앱이 ASR/TTS 엔드포인트 전체를 요구하면 각각 `/v1/audio/transcriptions`, `/v1/audio/speech`까지 넣으세요. **LLM base URL까지 이 주소로 바꾸면 채팅 기능은 404가 납니다.** 음성용 base URL을 따로 설정할 수 있는 클라이언트에 연결하세요. 브라우저 직접 호출용 CORS는 기본으로 켜지 않았습니다. 브라우저 앱이면 같은 출처의 백엔드에서 중계하고 키를 공개 프런트엔드에 하드코딩하지 않는 구성이 적합합니다.

기본 음성 매핑:

| client voice | Google voice |
|---|---|
| alloy | `GOOGLE_TTS_VOICE` 환경변수, 기본 ko-KR-Wavenet-A |
| nova / marin | ko-KR-Wavenet-A |
| shimmer / coral / sage | ko-KR-Wavenet-B |
| echo / fable / ash / ballad | ko-KR-Wavenet-C |
| onyx / verse / cedar | ko-KR-Wavenet-D |

Google 공식 목록상 A/B는 FEMALE, C/D는 MALE 표기입니다. 이는 OpenAI 원래 음성과 비슷하다는 뜻이 아닙니다.

## 7. 실제 연결 테스트

본인의 `.env`를 shell에 읽어 넣습니다. 아래 테스트는 실제 상위 API를 호출하므로 무료 한도 내 사용량에 포함됩니다.

```bash
set -a; source .env; set +a
export SPEECH_BASE_URL="http://${BIND_IP}:${PORT}/v1"

# 모델 목록: 외부 API 호출 없음
curl -fsS "$SPEECH_BASE_URL/models" \
  -H "Authorization: Bearer $PROXY_API_KEY"

# 음성 인식: 실제 녹음 파일로 바꾸기
curl --fail-with-body "$SPEECH_BASE_URL/audio/transcriptions" \
  -H "Authorization: Bearer $PROXY_API_KEY" \
  -F model=whisper-1 -F language=ko -F file=@recording.wav

# 음성 합성: HTTP 오류 시 오디오 파일로 오인하지 않도록 -f 사용
curl -fS "$SPEECH_BASE_URL/audio/speech" \
  -H "Authorization: Bearer $PROXY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"tts-1","voice":"alloy","input":"안녕하세요. 한국어 음성 테스트입니다.","response_format":"mp3"}' \
  -o speech.mp3

# Mac: afplay speech.mp3
# FFmpeg 설치 환경: ffplay -autoexit speech.mp3

curl -fsS "http://${BIND_IP}:${PORT}/usage" \
  -H "Authorization: Bearer $PROXY_API_KEY"
```

OpenAI Python SDK 예시는 `examples/client.py`에 있습니다. 별도 SDK 설치:

```bash
.venv/bin/pip install 'openai>=1.60,<3'
PROXY_API_KEY="$PROXY_API_KEY" SPEECH_BASE_URL="$SPEECH_BASE_URL" \
  .venv/bin/python examples/client.py recording.wav
```

SDK 예제는 `max_retries=0`으로 설정합니다. 일부 SDK/앱은 429/5xx/타임아웃을 기본 재시도하므로, 자동 재시도를 끄거나 제한하세요. 프록시의 메모리 캐시는 성공한 동일 요청을 절약하지만 실패/동시 요청의 exactly-once 실행을 보장하지 않습니다.

## 8. 오류 처리 / 운영

| 응답 | 확인할 내용 |
|---|---|
| 401 invalid_api_key | Groq 키가 아닌 로컬 PROXY_API_KEY를 클라이언트에 입력했는지 |
| 400 unsupported/model/voice | 위 호환 표에 없는 model·voice·옵션인지 |
| 413 | 파일 25,000,000바이트 또는 전체 request body 제한 초과 |
| 429 local_tts_limit | `/usage` 확인. 원장을 삭제해서 초기화하지 말 것 |
| 429 upstream_rate_limit | Groq/Google 계정 한도. 가능하면 Retry-After에 따를 것 |
| 502 upstream_auth_error | Groq 키 / Google API 활성화·결제·권한·프로젝트 확인 |
| 503 google_auth_failed | ADC 파일 내용·경로·컨테이너 읽기 권한 확인 |
| 503 usage_storage_error | SQLite 디렉터리 쓰기 권한·디스크·락 확인. 실패 시 상위 합성 차단 |
| 504 upstream_timeout | 네트워크/상위 지연. 실패에도 TTS 예약량은 유지됨 |

프록시는 transcript·입력 텍스트·오디오·키를 로그에 기록하지 않습니다. 상위 API에는 실제 음성/텍스트가 전송되므로 각 제공자의 데이터 정책이 적용됩니다. 업로드가 multipart 파서의 메모리 한도를 넘으면 임시 파일로 잠시 기록될 수 있습니다. 제공한 Docker 구성은 `/tmp`를 tmpfs로 사용하지만, Docker 없이 실행하면 OS 임시 디렉터리를 사용합니다. TTS 음성 캐시를 끄려면 `TTS_CACHE_MAX_BYTES=0`을 지정하세요.

검증 결과와 검증하지 못한 범위는 `TEST_REPORT.md`를 확인하세요.
