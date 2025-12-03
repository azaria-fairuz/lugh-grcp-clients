import os
import io
import cv2
import grpc
import threading
import frame_pb2
import frame_pb2_grpc

import numpy as np

from PIL import Image
from fastapi import FastAPI
from pydantic import BaseModel
from dotenv import load_dotenv
from typing import Any, Optional
from fastapi.responses import JSONResponse

load_dotenv(override=True)

GRPC_ADDRESS = os.getenv("GRPC_ADDRESS")

app = FastAPI()

threads = {}
running_streams = {}

class ConnectRequest(BaseModel):
    camera_id  : str
    rtsp_url   : str

class ResponseAPI(BaseModel):
    status      : str
    http_code   : int
    message     : Optional[str] = None
    data        : Optional[Any] = None

def stream_to_grpc(camera_id: str, rtsp_url: str):
    print(f"attempting to connect from to {rtsp_url}")

    channel = grpc.insecure_channel(GRPC_ADDRESS)
    stub = frame_pb2_grpc.FrameServiceStub(channel)

    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if not cap.isOpened():
        print(f"unable to connect to {rtsp_url}")
        running_streams[camera_id] = False
        return
    
    print(f"connected to {rtsp_url}")

    while running_streams[camera_id]:
        ret, frame = cap.read()
        if not ret:
            continue
        
        _, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        frame_bytes = buffer.tobytes()

        img = Image.open(io.BytesIO(frame_bytes))
        w, h = img.size

        message = frame_pb2.Frame(camera_id = camera_id, width = w, height = h, data = frame_bytes)

        try:
            stub.SendFrame(message)
        except Exception as e:
            print(f"unable to send frame data to grpc server")
            print(str(e))

    cap.release()
    running_streams[camera_id] = False
    print(f"the streaming data of camera with id of {camera_id} has ended")

def get_response_format(http_code: int, message: str = None, status: str = "success", data: Any = None):
    return ResponseAPI(
        status    = status,
        http_code = http_code,
        message   = message,
        data      = data,
    )

@app.post("/connect", response_model = ResponseAPI, response_model_exclude_none = True)
def start_camera_connection(req: ConnectRequest):
    camera_id = req.camera_id
    if camera_id in running_streams and running_streams[camera_id] == True:
        message = f"Camera {camera_id} is already connected"
        return get_response_format(200, message = message)
    
    running_streams[camera_id] = True
    t = threading.Thread(
        target=stream_to_grpc,
        args=(camera_id, req.rtsp_url),
        daemon=True
    )

    threads[camera_id] = t
    t.start()

    return get_response_format(200)

@app.post("/disconnect/{camera_id}", response_model = ResponseAPI, response_model_exclude_none = True)
def stop_camera_connection(camera_id: str):
    if camera_id not in running_streams:
        message  = f"connection with camera id of {camera_id} does not exist"
        response = get_response_format(200, message = message)
        
        return response

    running_streams[camera_id] = False

    return get_response_format(200)

@app.get("/status", response_model = ResponseAPI, response_model_exclude_none = True)
def stop_camera_connection():
    connection = [cam for cam, active in running_streams.items() if active]
    response   = get_response_format(200, data = connection)

    return response