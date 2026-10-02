"""Usage: PROXY_API_KEY=... python examples/client.py /path/to/recording.wav"""
import os
import sys
from pathlib import Path
from openai import OpenAI

client = OpenAI(
    base_url=os.getenv("SPEECH_BASE_URL", "http://127.0.0.1:8787/v1"),
    api_key=os.environ["PROXY_API_KEY"],
    max_retries=0,  # Avoid hidden duplicate billable synthesis after a timeout.
    timeout=300,
)
if len(sys.argv) > 1:
    with Path(sys.argv[1]).open("rb") as audio:
        text = client.audio.transcriptions.create(model="whisper-1", file=audio, language="ko")
    print(text.text)

with client.audio.speech.with_streaming_response.create(
    model="tts-1", voice="alloy", input="안녕하세요. 한국어 음성 연결 테스트입니다.", response_format="mp3",
) as response:
    response.stream_to_file("speech.mp3")
print("Created speech.mp3. Note: the gateway finishes synthesis before returning bytes.")
