# Korean Speech OpenAI Bridge

Paseo의 한국어 받아쓰기·음성 대화에 연결하는 OpenAI Audio API 호환 서버입니다. 음성 인식은 **Groq Whisper**, 음성 합성은 **Google Cloud 한국어 Standard/WaveNet**으로 처리합니다. Python 3.12, FastAPI, FFmpeg를 사용합니다.

```text
Paseo daemon / OpenAI SDK
          │  Bearer PROXY_API_KEY
          ▼
http://127.0.0.1:8787/v1
          ├─ /audio/transcriptions → Groq whisper-large-v3-turbo
          └─ /audio/speech         → Google Cloud Text-to-Speech
```

[공유 대화](https://chatgpt.com/share/6abfa690-cae0-83ee-906d-81718968bb90)와 사용자가 제공한 원본 ZIP의 코드·테스트를 검토해 보완했습니다. 기존 음성 별칭, 출력 형식, 속도 범위를 유지하면서 Paseo 설정 예제, Groq 사용량 제한, 지속 사용량 기록, 입력 검증과 테스트를 강화했습니다.

음성 전용 서버입니다. LLM의 채팅·Responses API, OpenAI Realtime API, WebSocket 부분 전사는 제공하지 않습니다. 기존 Codex/Claude 등 에이전트 설정은 그대로 사용하고 Paseo의 **음성 설정만** 이 서버로 연결합니다.

## 빠른 시작

필요한 것은 Python 3.12 이상과 FFmpeg 또는 Docker Compose, Groq API 키, Google Cloud Text-to-Speech용 API 키 또는 ADC 자격 증명입니다. Google 쪽은 해당 프로젝트의 Cloud Text-to-Speech API 활성화와 결제 연결이 필요합니다.

### uv 패키지로 설치

저장소를 직접 관리하지 않고 GitHub에서 패키지를 설치할 수 있습니다. Git, `ffmpeg`·`ffprobe`가 필요하며 Ubuntu/Debian에서는 `sudo apt-get install git ffmpeg`로 준비합니다. uv가 없다면 [공식 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)를 따라 먼저 설치합니다. 현재 PyPI에는 게시하지 않았으므로 아래 Git URL을 사용합니다.

```bash
uv tool install --python 3.12 'git+https://github.com/RunaticMoon/korean-speach-openai.git'
korean-speech-openai init
```

uv가 Python과 의존성을 독립된 환경에 설치하고 `korean-speech-openai` 명령을 제공합니다. 명령을 찾지 못하면 `uv tool update-shell`을 실행한 뒤 새 셸을 엽니다. [uv 도구 설치·관리 안내](https://docs.astral.sh/uv/guides/tools/).

`init`은 설정 파일 `~/.config/korean-speech-openai/server.env`를 권한 `600`으로 생성하고 `PROXY_API_KEY`에 난수를 넣습니다. 기존 파일은 덮어쓰지 않습니다. 설정 파일을 편집해 `GROQ_API_KEY`와 Google 인증 정보를 채우고, Groq Free 플랜을 직접 확인한 뒤 `GROQ_FREE_TIER_CONFIRMED=true`로 바꿉니다.

```bash
nano "${XDG_CONFIG_HOME:-$HOME/.config}/korean-speech-openai/server.env"
```

가장 간단한 Google 설정은 Cloud Console에서 발급한 Cloud TTS용 키를 `GOOGLE_API_KEY`에 넣는 것입니다. 이 방식은 `GOOGLE_CLOUD_PROJECT`와 ADC 파일이 필요하지 않습니다. 프로젝트와 과금 대상은 키에서 결정됩니다.

```dotenv
GOOGLE_API_KEY=본인의_Cloud_TTS_API_키
```

ADC를 사용하려면 `GOOGLE_API_KEY`를 비우고 [Google Cloud 인증](#google-cloud-인증)에 따라 `GOOGLE_CLOUD_PROJECT`와 ADC를 준비합니다. 패키지 기본 ADC 경로는 `~/.config/korean-speech-openai/google-adc.json`이며, 기존 ADC 파일의 절대 경로를 `GOOGLE_APPLICATION_CREDENTIALS`에 지정해도 됩니다. Google 인증 설정을 마친 뒤 실행합니다.

```bash
korean-speech-openai serve
```

기본 주소는 `http://127.0.0.1:8787/v1`입니다. 패키지 CLI는 실행 디렉터리와 무관하게 사용자 설정 파일을 읽고, 사용량을 `~/.local/state/korean-speech-openai/usage.sqlite3`에 보관합니다. `XDG_CONFIG_HOME`과 `XDG_STATE_HOME`을 지정한 경우 각각 해당 디렉터리를 기준으로 합니다. 설정 경로는 `init --config /절대/경로/server.env`, `serve --config /절대/경로/server.env`처럼 변경할 수 있습니다. 주소·포트는 `serve --host 127.0.0.1 --port 8787`로 지정합니다.

### Linux에서 서비스로 실행

`serve`로 실행 중이라면 `Ctrl+C`로 종료한 뒤 다음 명령을 실행합니다. 현재 사용자의 systemd 서비스를 등록하고 활성화·시작합니다.

```bash
korean-speech-openai install-service
korean-speech-openai status
curl --fail http://127.0.0.1:8787/health
```

`install-service`에도 `--config`, `--host`, `--port`를 지정할 수 있습니다. `--no-start`는 서비스 파일을 설치하고 자동 시작을 활성화하되 즉시 시작하지 않습니다. 수동 시작은 `systemctl --user start korean-speech-openai.service`로 수행합니다. 서비스 등록 명령은 시작을 요청하므로 `status`와 `/health`로 실제 실행 상태를 확인합니다. 서비스는 uv가 설치한 Python 환경을 사용하므로 해당 디렉터리를 이동하거나 삭제하지 않습니다. 외부 키를 아직 설정하지 않았다면 프로세스와 `/health`는 동작해도 음성 API를 사용할 수 없습니다.

서비스의 로그·재시작·중지는 다음과 같이 관리합니다. 설정 파일 변경 후에는 재시작합니다.

```bash
journalctl --user -u korean-speech-openai.service -n 50 --no-pager
systemctl --user restart korean-speech-openai.service
systemctl --user stop korean-speech-openai.service
# 자동 시작도 해제하려면:
systemctl --user disable --now korean-speech-openai.service
```

재부팅 후 로그인 전이나 로그아웃 후에도 실행하려면 사용자 linger가 필요합니다. 서버 관리 권한이 있는 사용자가 한 번 `sudo loginctl enable-linger "$(id -un)"`를 실행합니다. `install-service`는 이 시스템 설정이나 Paseo daemon 설정을 변경하지 않습니다.

업데이트할 때는 uv로 패키지를 갱신한 뒤 `install-service`를 다시 실행해 서비스 파일을 갱신하고 다시 시작합니다. 기존에 `--config`, `--host`, `--port`를 지정했다면 같은 옵션을 다시 전달합니다. 사용자 설정·ADC·사용량 DB는 패키지 환경 외부에 보존됩니다.

```bash
uv tool upgrade korean-speech-openai
korean-speech-openai install-service
korean-speech-openai status
curl --fail http://127.0.0.1:8787/health
```

### 소스 또는 Docker로 설치

소스를 수정하거나 Docker Compose를 사용하는 경우 아래 방식으로 준비합니다. 이 방식은 프로젝트 루트의 `.env`를 사용합니다.

```bash
git clone https://github.com/RunaticMoon/korean-speach-openai.git
cd korean-speach-openai
cp .env.example .env
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

출력한 난수를 `.env`의 `PROXY_API_KEY`에 넣고 나머지 값을 채웁니다. `PROXY_API_KEY`는 최소 32자이며, 클라이언트와 브리지 사이에서 쓰는 별도의 키입니다. Groq 키와 Google 자격 증명은 Paseo에 입력하지 않습니다.

```dotenv
PROXY_API_KEY=여기에_직접_생성한_32자_이상의_난수
GROQ_API_KEY=본인의_Groq_API_키
GROQ_FREE_TIER_CONFIRMED=true
GOOGLE_API_KEY=본인의_Cloud_TTS_API_키
```

ADC를 사용하는 경우에는 위의 `GOOGLE_API_KEY` 대신 다음 값을 설정합니다.

```dotenv
GOOGLE_API_KEY=
GOOGLE_CLOUD_PROJECT=본인의_Google_Cloud_프로젝트_ID
GOOGLE_APPLICATION_CREDENTIALS=./secrets/google-adc.json
```

**Groq 콘솔에서 해당 키의 조직이 Free 플랜인지 직접 확인한 뒤에만** `GROQ_FREE_TIER_CONFIRMED=true`로 설정합니다. 이 설정은 사용자 확인을 기록할 뿐 계정 플랜을 조회하거나 무료 플랜으로 전환하지 않습니다. 서버의 자체 한도와 별도로 실제 계정의 [Groq Limits](https://console.groq.com/settings/limits)가 적용됩니다.

### Google Cloud 인증

프로젝트에 결제를 연결하고 Cloud Text-to-Speech API를 활성화한 뒤 아래 두 방식 중 하나를 선택합니다.

#### Cloud TTS API 키

Cloud Console의 **API 및 서비스 → 사용자 인증 정보**에서 API 키를 만들고, API 제한에 **Cloud Text-to-Speech API**를 지정합니다. 서버의 고정 외부 IP를 사용하는 경우 해당 IP로 애플리케이션 제한도 설정할 수 있습니다. 키 값은 `server.env` 또는 `.env`의 `GOOGLE_API_KEY`에 저장합니다. [Google API 키 생성·제한 안내](https://docs.cloud.google.com/docs/authentication/api-keys).

`GOOGLE_API_KEY`가 있으면 서버는 Google 요청의 `x-goog-api-key` 헤더로 전달하며 ADC와 `GOOGLE_CLOUD_PROJECT`를 사용하지 않습니다. 기본 ADC 경로에 파일이 없어도 API 키 방식으로 실행됩니다. Google이 키를 거절하면 오류를 반환하고 ADC로 자동 재시도하지 않습니다. ADC로 전환하려면 키 값을 비우고 서비스를 재시작합니다. [Google API 키 사용 안내](https://docs.cloud.google.com/docs/authentication/api-keys-use).

Google AI Studio에서 Gemini용으로 준비한 키만으로 Cloud TTS 설정까지 완료된 것은 아닙니다. 해당 키 프로젝트의 Cloud TTS API 활성화·결제 연결·API 제한을 확인해야 합니다.

#### ADC

`GOOGLE_API_KEY`를 비우고 `GOOGLE_CLOUD_PROJECT`를 지정합니다. 로컬 개발용 사용자 ADC는 다음과 같이 준비할 수 있습니다.

```bash
gcloud auth login
gcloud services enable texttospeech.googleapis.com --project=YOUR_PROJECT_ID
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

`application-default login`이 출력하는 ADC 파일을 복사합니다. Linux/macOS의 기본 ADC 경로와 uv 설치의 기본 설정 경로를 사용하는 경우:

```bash
cp "$HOME/.config/gcloud/application_default_credentials.json" \
  "${XDG_CONFIG_HOME:-$HOME/.config}/korean-speech-openai/google-adc.json"
chmod 600 "${XDG_CONFIG_HOME:-$HOME/.config}/korean-speech-openai/google-adc.json"
```

소스·Docker 설치에서는 프로젝트 디렉터리의 `secrets/google-adc.json`을 사용합니다.

```bash
mkdir -p secrets
chmod 700 secrets
cp "$HOME/.config/gcloud/application_default_credentials.json" secrets/google-adc.json
chmod 600 secrets/google-adc.json
```

`YOUR_PROJECT_ID`와 `server.env` 또는 `.env`의 `GOOGLE_CLOUD_PROJECT`는 실제 프로젝트 ID로 바꿉니다. 사용자에게 해당 프로젝트의 API 사용 권한이 있어야 합니다. 다른 클라우드에서 상시 운영할 때는 전용 자격 증명과 Workload Identity Federation 등 운영 환경에 맞는 ADC 구성을 사용합니다. 설정 파일이 외부 토큰 파일을 참조한다면 그 파일도 컨테이너에서 접근할 수 있어야 합니다. [Google TTS 인증](https://docs.cloud.google.com/text-to-speech/docs/authentication), [환경별 ADC 설정](https://docs.cloud.google.com/docs/authentication/provide-credentials-adc).

ADC 방식의 자격 증명 탐색과 토큰 갱신은 실제 TTS 요청 시 수행됩니다. 두 인증 방식 모두 `/health` 또는 `/ready` 성공만으로 Google의 키 유효성·권한·결제·네트워크 연결까지 확인된 것은 아닙니다.

### Docker Compose 실행

기본 Compose 파일과 아래 예시는 **ADC 방식**을 사용합니다. API 키만 사용하려면 `.env`에 `GOOGLE_API_KEY`를 설정하고 `compose.yaml`에서 `./secrets/google-adc.json`의 bind mount와 `GOOGLE_APPLICATION_CREDENTIALS` 환경변수 항목을 제거합니다. 사용량을 보존하는 `speech-data` 볼륨은 유지하며 ADC 읽기 확인 단계는 생략합니다.

컨테이너는 **UID/GID 10001**로 실행됩니다. ADC 방식에서는 `secrets/google-adc.json`을 읽기 전용으로 마운트하며 이 UID가 읽을 수 있어야 합니다. 일반적인 Linux Docker에서는 ACL로 해당 UID에만 읽기 권한을 줄 수 있습니다.

```bash
# Linux에서 필요하면 먼저 배포판의 acl 패키지를 설치합니다.
setfacl -m u:10001:r secrets/google-adc.json
docker compose build
docker compose run --rm --no-deps speech-api python -c \
  'from pathlib import Path; assert Path("/run/secrets/google-adc.json").is_file(); Path("/run/secrets/google-adc.json").open("rb").close(); print("ADC readable")'
docker compose up -d
docker compose logs --tail=50 speech-api
curl --fail http://127.0.0.1:8787/health
```

Docker Desktop의 파일 공유 방식이나 rootless/user namespace 설정에서는 UID 매핑이 다를 수 있습니다. 위 읽기 확인이 실패하면 해당 환경의 파일 공유 권한을 조정합니다. 자격 증명을 모두에게 읽기 가능하게 만들거나 로그에 출력하지 않습니다.

Compose는 호스트의 `.env`를 읽되 ADC 경로를 `/run/secrets/google-adc.json`, 사용량 DB 경로를 `/app/data/usage.sqlite3`으로 덮어씁니다. 명명된 `speech-data` 볼륨은 처음 생성할 때 이미지의 UID 10001 소유 디렉터리로 초기화됩니다. 이미 다른 UID로 생성한 볼륨을 재사용한다면 기존 데이터를 보존한 채 소유권을 맞춰야 합니다.

컨테이너 루트 파일시스템은 읽기 전용이며 업로드 처리용 `/tmp`는 128MiB tmpfs입니다. 기본 메모리 한도는 768MiB, 프로세스 한도는 128, worker는 1개입니다. SQLite 볼륨만 지속적으로 쓰며 성공 오디오 캐시는 메모리에 있습니다. 장문·동시 요청에 맞춰 한도를 높일 때는 호스트의 여유 자원도 함께 확인합니다.

기본 공개 주소는 `127.0.0.1:8787`입니다. 원격 Paseo daemon에서 접근해야 한다면 `.env`의 `BIND_IP`를 해당 서버의 Tailscale/VPN 주소로 바꾸고 서버를 재생성합니다. `PORT`는 호스트 포트이며 컨테이너 내부 포트는 8787입니다. 인터넷에 직접 노출할 때는 HTTPS와 접근 제한을 별도로 구성해야 합니다.

### Python으로 직접 실행

Ubuntu/Debian에서는 `sudo apt-get install ffmpeg`로 FFmpeg와 ffprobe를 설치합니다. 다른 OS에서는 사용하는 패키지 관리자로 설치한 뒤 두 실행 파일이 `PATH`에 있는지 확인합니다.

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn speech_proxy.app:app --host 127.0.0.1 --port 8787
```

프로젝트 루트에서 실행하면 `.env` 설정을 읽습니다. 직접 실행할 때는 `data/` 디렉터리에 현재 사용자의 쓰기 권한이 있어야 하며, ADC 방식이라면 ADC 파일을 읽을 수 있어야 합니다. `USAGE_DB_PATH`의 부모 디렉터리는 자동 생성됩니다.

`.env`의 `BIND_IP`와 `PORT`까지 적용하려면 `./scripts/run-local.sh`를 실행합니다. 원본의 `uvicorn app.main:create_app --factory` 진입점도 유지합니다.

### 원본 ZIP에서 업데이트

기존 `.env`, `secrets/google-adc.json`, `data/usage.sqlite3` 또는 `speech-data` 볼륨을 보존합니다. 새 설정 중 `GROQ_FREE_TIER_CONFIRMED=true`는 Free 플랜을 직접 확인한 뒤 추가해야 합니다. 기존 `TTS_ROLLING_32D_CHAR_LIMIT`, `TTS_ROLLING_24H_CHAR_LIMIT`, `TTS_CACHE_MAX_BYTES`, `TTS_CACHE_TTL_SECONDS`, `DATA_DIR`, `ASR_DEFAULT_LANGUAGE`도 호환되며 새 배포에는 `.env.example`의 표준 이름을 권장합니다.

기존 DB의 TTS `reservations` 기록은 새 사용량 원장에 한 번만 가져옵니다. 기존 테이블은 보존합니다. 업데이트 전 DB를 백업하고 기존 프로세스를 중지한 뒤 새 버전을 시작합니다. 구버전과 신버전을 동시에 실행하면 가져오기 이후의 구버전 예약이 새 원장에 반영되지 않으므로 함께 실행하지 않습니다. Python 직접 실행과 Compose가 서로 다른 DB를 사용하지 않는지도 확인합니다.

## Paseo 연결

설정 대상은 휴대폰·데스크톱 UI가 아니라 **실제로 실행 중인 Paseo daemon의 설정 파일**입니다. 기본 위치는 `~/.paseo/config.json`이며 `PASEO_HOME`을 변경했다면 그 홈을 사용합니다.

[examples/paseo.config.json](examples/paseo.config.json)의 내용을 기존 설정에 병합하고 `REPLACE_WITH_YOUR_PROXY_API_KEY` 두 곳을 `server.env` 또는 `.env`의 `PROXY_API_KEY`로 바꿉니다. 기존 `agents`, `daemon`, `features.voiceMode.llm` 등의 설정을 덮어쓰지 않습니다.

```json
{
  "version": 1,
  "features": {
    "dictation": {
      "stt": {"provider": "openai", "model": "whisper-1", "language": "ko"}
    },
    "voiceMode": {
      "stt": {"provider": "openai", "model": "whisper-1", "language": "ko"},
      "tts": {"provider": "openai", "model": "tts-1", "voice": "alloy"}
    }
  },
  "providers": {
    "openai": {
      "stt": {
        "apiKey": "REPLACE_WITH_YOUR_PROXY_API_KEY",
        "baseUrl": "http://127.0.0.1:8787/v1"
      },
      "tts": {
        "apiKey": "REPLACE_WITH_YOUR_PROXY_API_KEY",
        "baseUrl": "http://127.0.0.1:8787/v1"
      }
    }
  }
}
```

- `baseUrl`은 **Paseo daemon에서 접근 가능한 주소**이며 `/v1`을 한 번 포함합니다. 두 서버가 다른 머신에 있으면 `127.0.0.1` 대신 브리지 서버의 주소를 씁니다. Paseo 자체가 컨테이너에 있으면 그 컨테이너의 `127.0.0.1`은 브리지가 아닙니다.
- `providers.openai.stt`/`tts`는 음성 전용 설정입니다. 기존 LLM의 `OPENAI_BASE_URL`을 이 주소로 바꾸지 않습니다.
- 받아쓰기와 음성 대화의 언어를 모두 `ko`로 설정합니다. Paseo 기본 언어는 `en`입니다.
- Paseo에는 TTS 모델 `tts-1`, 음성 `alloy` 등 OpenAI 별칭을 입력합니다. Google 음성 ID를 Paseo의 `voice` 필드에 직접 넣으면 Paseo의 설정 검증에서 거절됩니다.
- Paseo의 PCM 요청에는 **24kHz, mono, signed 16-bit little-endian, 헤더 없는 PCM**을 반환합니다. WAV/MP3 바이트를 PCM으로 반환하지 않습니다.

설정 적용은 진행 중인 에이전트 작업이 끝난 뒤 수행합니다. 직접 파일을 편집한 경우 `paseo reload`로 검증할 수 있지만 **음성·자격 증명 변경은 daemon 재시작이 필요합니다**. CLI 관리 daemon은 `paseo daemon restart`를 사용하고, Desktop 관리 daemon은 해당 Desktop의 관리 방식으로 다시 시작합니다. Docker 배포는 해당 컨테이너를 다시 시작합니다. 이 저장소의 설치 과정은 Paseo 설정을 자동 수정하거나 daemon을 재시작하지 않습니다.

Managed start/Desktop에서는 상속된 daemon 환경변수를 제거할 수 있으므로 음성 설정을 `config.json`에 저장합니다. 현재 계약은 [Paseo Voice](https://paseo.sh/docs/voice.md)와 [Configuration](https://paseo.sh/docs/configuration.md)을 기준으로 하며, 설치된 Paseo 버전이 오래되었다면 별도 STT/TTS 주소 설정 지원 여부를 확인합니다.

## API와 사용 예제

`GET /health`만 인증 없이 호출할 수 있습니다. `/v1/*`, `/usage`, `/ready`에는 `Authorization: Bearer <PROXY_API_KEY>`가 필요합니다.

| 메서드·경로 | 동작 |
|---|---|
| `GET /health` | 프로세스 생존 확인. 외부 API를 호출하지 않음 |
| `GET /ready` | 설정 준비 상태. 외부 API나 Google 토큰 발급을 시험하지 않음 |
| `GET /usage` | SQLite에 기록된 예약 사용량과 한도 조회 |
| `GET /v1/models` | 지원하는 호환 모델 목록 |
| `GET /v1/voices` | OpenAI 별칭과 Google 음성 매핑. 이 서버의 확장 API |
| `POST /v1/audio/transcriptions` | multipart 음성 인식 |
| `POST /v1/audio/speech` | JSON 텍스트 입력을 음성으로 변환 |

SDK 예제를 실행하려면 개발 의존성을 설치합니다. [examples/client.py](examples/client.py)는 `.env`를 읽으며 재시도에 따른 중복 사용량을 줄이기 위해 `max_retries=0`을 지정합니다.

```bash
python -m pip install -r requirements-dev.txt
python examples/client.py transcribe recording.wav
python examples/client.py speak '안녕하세요. 한국어 음성 연결 테스트입니다.' --output speech.mp3
python examples/client.py speak 'PCM 테스트입니다.' --format pcm --output speech.pcm
```

다른 서버를 지정할 때는 `python examples/client.py --base-url http://SERVER:8787/v1 speak '안녕하세요'`처럼 공통 옵션을 하위 명령 앞에 둡니다. 직접 API 호출 예시:

```bash
# 별도 셸에서 설정합니다. .env 내용 전체를 출력하지 않습니다.
read -r -s -p 'PROXY_API_KEY: ' PROXY_API_KEY
export PROXY_API_KEY
curl --fail-with-body http://127.0.0.1:8787/ready \
  -H "Authorization: Bearer ${PROXY_API_KEY}"
curl --fail-with-body http://127.0.0.1:8787/usage \
  -H "Authorization: Bearer ${PROXY_API_KEY}"
curl --fail-with-body http://127.0.0.1:8787/v1/audio/transcriptions \
  -H "Authorization: Bearer ${PROXY_API_KEY}" \
  -F file=@recording.wav -F model=whisper-1 -F language=ko
curl --fail-with-body http://127.0.0.1:8787/v1/audio/speech \
  -H "Authorization: Bearer ${PROXY_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"model":"tts-1","voice":"alloy","input":"안녕하세요.","response_format":"wav"}' \
  --output speech.wav
```

### 지원 범위

| 항목 | 지원 |
|---|---|
| STT 모델 | `whisper-1` → `GROQ_MODEL`로 설정한 모델, 기본 Turbo; `whisper-large-v3-turbo`, `whisper-large-v3` 직접 지정 가능 |
| STT 언어 | 기본 `ko`; `ASR_DEFAULT_LANGUAGE`로 변경, `auto`는 자동 감지 확장값 |
| STT 응답 | `json`, `text`, `verbose_json`, `srt`, `vtt` |
| TTS 모델 | `tts-1`, `tts-1-hd`, `google-wavenet` → 동일 Google 합성 경로 |
| TTS 형식 | `mp3`, `wav`, `pcm`, `opus`, `aac`, `flac` |
| TTS `speed` | `0.25`–`4.0`. `2.0` 초과는 Google 속도 `2.0`에 FFmpeg `atempo` 추가 적용 |
| TTS 입력 | 기본 최대 20,000자, 한국어 장문 UTF-8 바이트 단위 분할 |

Groq가 직접 제공하는 전사 형식은 `json`, `text`, `verbose_json`입니다. 이 서버의 `srt`/`vtt`는 Groq의 `verbose_json` segment 타임스탬프를 이용해 로컬에서 생성합니다. [Groq STT 문서](https://console.groq.com/docs/speech-to-text).

Google TTS 입력은 UTF-8 4,500바이트 이하로 분할하여 각 조각을 합성하고 PCM으로 결합합니다. 결과는 FFmpeg로 요청한 실제 컨테이너/코덱으로 변환합니다. `opus`는 Ogg/Opus, `aac`는 ADTS AAC입니다. `pcm`에는 WAV 헤더가 없으며 다른 형식은 해당 파일 형식으로 반환됩니다. 서버는 전체 합성과 변환을 마친 뒤 응답하므로 SDK의 `with_streaming_response`를 사용해도 생성 중 저지연 스트리밍은 아닙니다.

| 요청 `voice` | Google 음성 |
|---|---|
| `alloy` | `GOOGLE_TTS_VOICE` 값, 기본 `ko-KR-Wavenet-A` |
| `nova`, `marin` | `ko-KR-Wavenet-A` |
| `shimmer`, `coral`, `sage` | `ko-KR-Wavenet-B` |
| `echo`, `fable`, `ash`, `ballad` | `ko-KR-Wavenet-C` |
| `onyx`, `verse`, `cedar` | `ko-KR-Wavenet-D` |

브리지에 직접 요청할 때는 `ko-KR-Standard-A`–`D`, `ko-KR-Wavenet-A`–`D`도 허용합니다. 허용 목록 밖의 음성으로 자동 변경하지 않습니다. 별칭은 요청 형식을 맞추기 위한 것이며 OpenAI의 실제 목소리나 `tts-1-hd` 음질을 재현한다는 의미가 아닙니다.

## 사용량 제한과 운영

사용량은 외부 호출 전에 SQLite 트랜잭션으로 예약합니다. 같은 로컬 DB 파일을 공유하는 프로세스들은 합산된 한도를 적용받습니다. TTS는 원본과 동일하게 UTF-16 단위를 사용하므로 일반 한글은 1, 일부 보조 Unicode 문자·이모지는 2로 셉니다. `/usage`는 실제 청구량이 아닌 보수적인 예약량입니다. 실패·타임아웃·취소 후에는 공급자가 처리했을 가능성이 있으므로 예약량을 환급하지 않습니다. 사용량 DB가 쓰기 불가능하면 우회 호출하지 않습니다.

| 환경변수 | 기본값 | 의미 |
|---|---:|---|
| `TTS_32DAY_CHAR_LIMIT` | `3500000` | 최근 32일 TTS 문자 예약 상한 |
| `TTS_DAILY_CHAR_LIMIT` | `150000` | 최근 24시간 TTS 문자 예약 상한 |
| `GROQ_MINUTE_REQUEST_LIMIT` | `20` | 최근 60초 전사 요청 상한 |
| `GROQ_DAILY_REQUEST_LIMIT` | `2000` | 최근 24시간 전사 요청 상한 |
| `GROQ_HOURLY_AUDIO_SECONDS_LIMIT` | `7200` | 최근 1시간 음성 초 예약 상한 |
| `GROQ_DAILY_AUDIO_SECONDS_LIMIT` | `28800` | 최근 24시간 음성 초 예약 상한 |
| `MAX_AUDIO_SECONDS` | `600` | 업로드 한 건의 최대 음성 길이 |
| `MAX_TTS_INPUT_CHARS` | `20000` | 한 건의 최대 TTS 입력 문자 수 |
| `CACHE_MAX_BYTES` | `33554432` | 프로세스당 성공 PCM 메모리 캐시 최대 바이트 |
| `CACHE_TTL_SECONDS` | `3600` | 성공 PCM 캐시 유지 시간 |
| `USAGE_DB_PATH` | `data/usage.sqlite3` | 직접 실행 시 사용량 DB 경로 |

기본 상한은 공급자의 공식 무료 한도를 보장하거나 실시간 동기화하는 값이 아닙니다. 한도를 넘으면 외부 요청 전에 `429`를 반환하며 유료 모델로 자동 전환하지 않습니다. 동일 요청의 성공 PCM이 캐시에 있으면 외부 합성 및 사용량 예약을 생략합니다. 캐시는 프로세스마다 별도이며 재시작하면 사라집니다. 여러 worker 또는 여러 서버의 캐시를 공유하지 않습니다.

사용량 한도 `0`은 해당 공급자 호출 차단을 뜻하며 무제한 설정이 아닙니다. `TTS_32DAY_CHAR_LIMIT`는 최대 350만 단위까지 허용합니다. Groq 음성 길이는 파일 메타데이터만 신뢰하지 않고 디코딩한 뒤 측정하며, 요청마다 최소 10초를 예약하고 그 이상은 초 단위로 올림합니다. 짧은 받아쓰기 여러 건도 각각 최소 예약량을 사용합니다. 캐시를 끄려면 `CACHE_MAX_BYTES=0`으로 설정합니다.

**무과금을 보장하지 않습니다.** 이 서버는 같은 Google 결제 계정·Groq 조직에서 다른 앱이나 서버가 쓴 사용량을 알 수 없습니다. 이전 사용량, 공급자의 과금 단위·정책 변경도 따로 확인해야 합니다. 자신의 [Google TTS 가격](https://cloud.google.com/text-to-speech/pricing)과 [Groq 한도](https://console.groq.com/docs/rate-limits)를 확인하고 필요하면 자체 상한을 더 낮춥니다.

DB는 로컬 디스크에 보관합니다. SQLite 파일을 NFS 같은 네트워크 파일시스템으로 공유하거나 서로 다른 DB를 쓰는 복제 서버에서 계정 전체 한도가 합산된다고 가정하지 않습니다. `speech-data` 볼륨이나 DB를 지우면 예약 기록도 사라지므로 **`docker compose down -v`로 볼륨을 삭제하지 마세요.** 백업은 SQLite backup API 또는 서버를 중지한 상태에서 수행하고 WAL 파일만 누락한 단순 복사는 피합니다.

키, ADC 파일, 실제 녹음, 생성 음성을 저장소에 올리지 않습니다. 공유 인증키 하나를 사용하는 개인용 배포를 기본 대상으로 하며 사용자별 과금·권한 분리는 제공하지 않습니다.

음성 인식 요청의 multipart 파싱과 디코딩 검증에는 임시 오디오 파일이 사용될 수 있으며 처리 후 삭제합니다. Compose의 임시 파일은 `/tmp` tmpfs에, Python 직접 실행에서는 OS 임시 디렉터리에 생성됩니다. 영구 녹음 기능은 없으며 사용량 DB에는 시각·공급자·예약 단위가 저장됩니다. 실제 음성·합성 텍스트는 각각 Groq·Google에 전송되므로 해당 공급자의 데이터 정책이 적용됩니다.

## 검증

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m pytest
docker build -t korean-speech-openai:test .
```

테스트는 공급자 HTTP를 모의 응답으로 대체하고 실제 FFmpeg/ffprobe를 사용합니다. 인증, 입력 검증, 오디오 변환, 한국어 분할, SQLite 사용량 제한, OpenAI SDK 요청/응답 호환성을 검증하는 범위입니다. CI는 Python 3.12에서 검사·테스트, Docker 이미지 빌드, 외부 자격 증명 없이 컨테이너를 시작하는 생존 확인을 수행하도록 구성했습니다.

실행 결과와 미검증 범위는 [TEST_REPORT.md](TEST_REPORT.md)에 기록합니다.

자동 테스트는 실제 공급자 키를 사용하지 않습니다. 유효한 사용자 자격 증명을 설정한 뒤 짧은 녹음과 TTS 입력으로 실제 Groq/Google 호출을 확인하고, Paseo의 마이크 입력과 스피커 재생까지 별도로 검증해야 합니다. ADC 방식은 Google 토큰 발급도 확인합니다. 공유 대화에 기재된 과거 테스트 개수나 배포 성공 여부는 이 저장소의 검증 결과로 간주하지 않습니다.

## 문제 해결

| 증상 | 확인할 내용 |
|---|---|
| `401` | Paseo/SDK의 키가 `PROXY_API_KEY`와 같은지, Bearer 헤더가 있는지 |
| 준비 상태 실패 | Groq 키·Free 플랜 확인 설정, Google API 키 또는 ADC·프로젝트 설정, FFmpeg 설치 |
| Google 인증 실패 | API 키 방식은 키의 Cloud TTS API 제한·애플리케이션 제한·프로젝트 결제 상태, ADC 방식은 파일 읽기 권한·quota project·프로젝트 권한 확인 |
| `429` | `/usage`의 예약량과 공급자 한도. 재시도마다 사용량이 추가될 수 있음 |
| 한국어가 영어로 잘못 전사됨 | Paseo dictation과 voiceMode의 `language`가 모두 `ko`인지 |
| TTS가 잡음이거나 속도가 다름 | 요청 형식 `pcm`과 24kHz PCM 계약, 잘못된 프록시 경로나 오래된 서버 이미지 |
| Paseo에 연결되지 않음 | daemon에서 브리지 주소 접근 가능 여부, `/v1` 중복·누락, 설정 적용 후 daemon 재시작 |

로그를 공유할 때는 인증키, ADC 내용, 음성·전사 텍스트를 제거합니다.
