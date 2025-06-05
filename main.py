import os
import json
import base64
import asyncio
import websockets
import traceback
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT', 10000))

SYSTEM_MESSAGE = (
    "こんにちは。私は自動応答AIアシスタントです。"
    "フロントガラス交換に関するご用件をお話しください。"
    "できるかぎり丁寧にお答えしますので、どうぞお話しください。"
)

VOICE = 'onyx'

app = FastAPI()

if not OPENAI_API_KEY:
    raise ValueError("OpenAI APIキーが設定されていません。")

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}

@app.api_route("/incoming-call", methods=["GET", "POST", "HEAD"])
async def handle_incoming_call(request: Request):
    response = VoiceResponse()
    response.pause(length=1)
    response.say("通話をAIアシスタントに接続します。少々お待ちください。", language="ja-JP")
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f"wss://{host}/media-stream")
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    print("Twilio からの WebSocket 接続を受け付けました")
    await websocket.accept()

    try:
        print("OpenAI WebSocket に接続中…")
        async with websockets.connect(
            'wss://api.openai.com/v1/realtime',
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "assistants=v2;realtime=v1"
            }
        ) as openai_ws:
            print("OpenAI WebSocket 接続成功！")
            await initialize_session(openai_ws)

            stream_sid = None
            latest_media_timestamp = 0
            last_assistant_item = None
            mark_queue = []

            async def receive_from_twilio():
                nonlocal stream_sid, latest_media_timestamp
                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        if data['event'] == 'media' and openai_ws.open:
                            latest_media_timestamp = int(data['media']['timestamp'])
                            audio_append = {
                                "type": "input_audio",
                                "audio": data['media']['payload']
                            }
                            await openai_ws.send(json.dumps(audio_append))
                        elif data['event'] == 'start':
                            stream_sid = data['start']['streamSid']
                            print(f"Incoming stream started: {stream_sid}")
                        elif data['event'] == 'mark' and mark_queue:
                            mark_queue.pop(0)
                except WebSocketDisconnect:
                    print("クライアント切断")
                    if openai_ws.open:
                        await openai_ws.close()

            async def send_to_twilio():
                nonlocal stream_sid, last_assistant_item
                try:
                    async for openai_message in openai_ws:
                        response = json.loads(openai_message)
                        if response.get('type') == 'audio' and 'audio' in response:
                            audio_payload = base64.b64encode(
                                base64.b64decode(response['audio'])
                            ).decode('utf-8')
                            audio_delta = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {
                                    "payload": audio_payload
                                }
                            }
                            await websocket.send_json(audio_delta)
                except Exception as e:
                    print(f"send_to_twilio エラー: {e}")
                    traceback.print_exc()

            await asyncio.gather(receive_from_twilio(), send_to_twilio())

    except Exception as e:
        print(f"OpenAI WebSocket 接続に失敗: {e}")
        traceback.print_exc()

async def initialize_session(openai_ws):
    print("初期セッションを送信中…")
    session_create = {
        "type": "session.create",
        "session": {
            "model": "gpt-4o",
            "voice": VOICE,
            "language": "ja",
            "instructions": SYSTEM_MESSAGE,
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "turn_detection": {"type": "server"}
        }
    }
    await openai_ws.send(json.dumps(session_create))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
