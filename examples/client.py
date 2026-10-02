"""OpenAI SDK로 실행 중인 브리지의 STT 또는 TTS를 호출합니다."""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("SPEECH_BASE_URL", "http://127.0.0.1:8787/v1"),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    transcribe = commands.add_parser("transcribe", help="음성 파일을 한국어로 전사")
    transcribe.add_argument("file", type=Path)
    transcribe.add_argument("--language", default="ko")
    speak = commands.add_parser("speak", help="한국어 텍스트를 음성 파일로 저장")
    speak.add_argument("text")
    speak.add_argument("--output", type=Path, default=Path("speech.mp3"))
    speak.add_argument("--voice", default="alloy")
    speak.add_argument(
        "--format", choices=("mp3", "wav", "pcm", "opus", "aac", "flac"), default="mp3"
    )
    args = parser.parse_args()
    api_key = os.getenv("PROXY_API_KEY")
    if not api_key:
        parser.error("PROXY_API_KEY를 환경변수 또는 .env 파일에 설정하세요.")

    # 응답이 불확실한 요청을 SDK가 자동 재전송해 사용량을 다시 예약하지 않도록 합니다.
    with OpenAI(base_url=args.base_url, api_key=api_key, max_retries=0, timeout=300) as client:
        if args.command == "transcribe":
            with args.file.open("rb") as audio:
                result = client.audio.transcriptions.create(
                    model="whisper-1", file=audio, language=args.language
                )
            print(result.text)
        else:
            with client.audio.speech.with_streaming_response.create(
                model="tts-1",
                voice=args.voice,
                input=args.text,
                response_format=args.format,
            ) as response:
                response.stream_to_file(args.output)
            print(f"저장 완료: {args.output}")


if __name__ == "__main__":
    main()
