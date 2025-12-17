import os
import io
import cv2
import grpc
import secrets
import threading
import frame_pb2
import frame_pb2_grpc

from jose import jwt, JWTError
from datetime import datetime, timedelta
from pony.orm import db_session, commit, select
from models import db, GaugeCalibration, GaugeType, CctvConnection, User

from PIL import Image
from pydantic import BaseModel
from dotenv import load_dotenv
from typing import Any, Optional
from fastapi.responses import JSONResponse
from fastapi import FastAPI, HTTPException, Query, Response, Request, Depends
from fastapi import APIRouter, Depends

load_dotenv(override=True)

db_file = "db.sqlite"
db_exists = os.path.exists(db_file)

db.bind(provider='sqlite', filename=db_file, create_db=not db_exists)
db.generate_mapping(create_tables=True)

@db_session
def seed_users():
    users = [
        {"name": "admin", "email": "admin@admin.com", "password": "adminadmin"},
        {"name": "adminpdu", "email": "adminpdu@admin.com", "password": "adminadmin"},
    ]

    for u in users:
        if not User.exists(email=u["email"]):
            db_user = User(name=u["name"],email=u["email"],password=u["password"])
            db_user.set_password(u["password"])

    print("[RTSP] Users seeded successfully!")

WITS_IP      = os.getenv("WITS_IP", 8504)
WITS_PORT    = os.getenv("WITS_PORT", "127.0.0.1")
GRPC_ADDRESS = os.getenv("GRPC_ADDRESS", 8501)
SECRET_KEY   = os.getenv("JWT_SECREET", "j192y9e7127")
ALGORITHM    = os.getenv("JWT_ALGORITHM", "HS256")

ACCESS_TOKEN_EXPIRE_MINUTES = os.getenv("JWT_EXPIRE_IN_MINUTE", 60)

class ConnectRequest(BaseModel):
    camera_id  : str
    rtsp_url   : str

class CalibrationRequest(BaseModel):
    gauge_type      : int
    cctv_connection : int

class CalibrationTypeRequest(BaseModel):
    max_value    : int
    min_value    : int
    start_degree : int
    end_degree   : int
    needle_type  : str

class CalibrationCCTVRequest(BaseModel):
    url      : str
    name     : str
    user     : str
    password : str

class LoginRequest(BaseModel):
    email    : str
    password : str

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

@db_session
def get_current_user(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        print(token)
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user = User.get(id=user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user

threads = {}
running_streams = {}
wits0_connection = False

app = FastAPI()
router = APIRouter(dependencies=[Depends(get_current_user)], tags=["Protected"])

seed_users()

#-- GRPC endpoints

@router.post("/connect", response_model = ResponseAPI, response_model_exclude_none = True)
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

@router.post("/disconnect/{camera_id}", response_model = ResponseAPI, response_model_exclude_none = True)
def stop_camera_connection(camera_id: str):
    if camera_id not in running_streams:
        message  = f"connection with camera id of {camera_id} does not exist"
        response = get_response_format(200, message = message)
        
        return response

    running_streams[camera_id] = False

    return get_response_format(200)

@router.get("/status", response_model = ResponseAPI, response_model_exclude_none = True)
def stop_camera_connection():
    connection = [cam for cam, active in running_streams.items() if active]
    response   = get_response_format(200, data = connection)

    return response

#-- CALIBRATION endpoints

@router.get("/calibration", response_model=ResponseAPI, response_model_exclude_none=True)
@db_session
def get_calibration(gauge_type: int = None, cctv_connection: int = None, page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100)):
    query = select(cal for cal in GaugeCalibration)

    if gauge_type is not None:
        query = query.filter(lambda cal: cal.gauge_type.id == gauge_type)
    if cctv_connection is not None:
        query = query.filter(lambda cal: cal.cctv_connection.id == cctv_connection)

    start = (page - 1) * page_size
    end   = start + page_size
    data  = [
        {
            "id": cal.id,
            "gauge_type": {
                "max_value": cal.gauge_type.max_value,
                "min_value": cal.gauge_type.min_value,
                "start_degree": cal.gauge_type.start_degree,
                "end_degree": cal.gauge_type.end_degree,
                "needle_type": cal.gauge_type.needle_type
            },
            "cctv_connection": {
                "url": cal.cctv_connection.url,
                "name": cal.cctv_connection.name,
                "user": cal.cctv_connection.user,
                "password": cal.cctv_connection.password
            }
        }
        for cal in query[start:end]
    ]

    total    = query.count() if hasattr(query, "count") else len(query)
    response = get_response_format(200, data = {"data" : data, "page": page, "page_size": page_size, "total": total})

    return response

@router.get("/calibration/cctv", response_model=ResponseAPI, response_model_exclude_none=True)
@db_session
def get_calibration_cctv(page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100)):
    query = CctvConnection.select()
    start = (page - 1) * page_size
    end   = start + page_size
    data  = [
        {
            "id"       : cal.id,
            "url"      : cal.url,
            "name"     : cal.name,
            "user"     : cal.user,
            # "password" : cal.password
        }
        for cal in query[start:end]
    ]

    total    = query.count() if hasattr(query, "count") else len(query)
    response = get_response_format(200, data = {"data" : data, "page": page, "page_size": page_size, "total": total})

    return response

@router.get("/calibration/type", response_model=ResponseAPI, response_model_exclude_none=True)
@db_session
def get_calibration_type(page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100)):
    query = GaugeType.select()
    start = (page - 1) * page_size
    end   = start + page_size
    data  = [
        {
            "id"           : cal.id,
            "max_value"    : cal.max_value,
            "min_value"    : cal.min_value,
            "start_degree" : cal.start_degree,
            "end_degree"   : cal.end_degree,
            "needle_type"  : cal.needle_type
        }
        for cal in query[start:end]
    ]

    total    = query.count() if hasattr(query, "count") else len(query)
    response = get_response_format(200, data = {"data" : data, "page": page, "page_size": page_size, "total": total})
    
    return response

@router.post("/calibration", response_model = ResponseAPI, response_model_exclude_none = True)
@db_session
def store_calibration(req: CalibrationRequest):
    query    = GaugeCalibration(gauge_type = req.gauge_type, cctv_connection = req.cctv_connection)
    response = get_response_format(200, data = {"gauge_type": query.gauge_type.id, "cctv_connection": query.cctv_connection.id})

    return response

@router.post("/calibration/cctv", response_model = ResponseAPI, response_model_exclude_none = True)
@db_session
def store_calibration_cctv(req: CalibrationCCTVRequest):
    query = CctvConnection(
        url      = req.url,
        name     = req.name,
        user     = req.user,
        password = req.password
    )
    data = {
        "url"      : query.url,
        "name"     : query.name,
        "user"     : query.user,
        # "password" : query.password
    }
    response = get_response_format(200, data = data)

    return response

@router.post("/calibration/type", response_model = ResponseAPI, response_model_exclude_none = True)
@db_session
def store_calibration_type(req: CalibrationTypeRequest):
    query = GaugeType(
        max_value    = req.max_value, 
        min_value    = req.min_value, 
        start_degree = req.start_degree, 
        end_degree   = req.end_degree, 
        needle_type  = req.needle_type
    )
    data = {
        "max_value"    : query.max_value, 
        "min_value"    : query.min_value, 
        "start_degree" : query.start_degree, 
        "end_degree"   : query.end_degree, 
        "needle_type"  : query.needle_type
    }
    response = get_response_format(200, data = data)

    return response

@router.put("/calibration/{id}", response_model = ResponseAPI, response_model_exclude_none = True)
@db_session
def update_calibration(id: int, req: CalibrationRequest):
    query = GaugeCalibration.get(id=id)
    if not query:
        raise HTTPException(status_code = 404, detail = "Item not found")
    
    if req.gauge_type is not None:
        query.gauge_type = req.gauge_type
    if req.cctv_connection is not None:
        query.cctv_connection = req.cctv_connection
    
    data = {
        "id"              : query.id,
        "gauge_type"      : query.gauge_type.id, 
        "cctv_connection" : query.cctv_connection.id
    }
    response = get_response_format(200, data = data)

    return response

@router.put("/calibration/type/{id}", response_model = ResponseAPI, response_model_exclude_none = True)
@db_session
def update_calibration_type(id: int, req: CalibrationTypeRequest):
    query = GaugeType.get(id=id)
    if not query:
        raise HTTPException(status_code = 404, detail = "Item not found")
    
    if req.max_value is not None:
        query.max_value = req.max_value
    if req.min_value is not None:
        query.min_value = req.min_value
    if req.start_degree is not None:
        query.start_degree = req.start_degree
    if req.end_degree is not None:
        query.end_degree = req.end_degree
    if req.needle_type is not None:
        query.needle_type = req.needle_type

    data = {
        "id"           : query.id,
        "max_value"    : query.max_value,
        "min_value"    : query.min_value,
        "start_degree" : query.start_degree,
        "end_degree"   : query.end_degree,
        "needle_type"  : query.needle_type
    }
    response = get_response_format(200, data = data)

    return response

@router.delete("/calibration/{id}", response_model = ResponseAPI, response_model_exclude_none = True)
@db_session
def delete_calibration(id: int):
    query = GaugeCalibration.get(id = id)
    if not query:
        raise HTTPException(status_code=404, detail="Item not found")
    
    query.delete()

    response = get_response_format(200, message = f"calibration with id of {id} has been deleted")

    return response 

@router.delete("/calibration/type/{id}", response_model = ResponseAPI, response_model_exclude_none = True)
@db_session
def delete_calibration_type(id: int):
    query = GaugeType.get(id = id)
    if not query:
        raise HTTPException(status_code=404, detail="Item not found")
    
    query.delete()
    response = get_response_format(200, message = f"calibration type with id of {id} has been deleted")

    return response 

@router.delete("/calibration/cctv/{id}", response_model = ResponseAPI, response_model_exclude_none = True)
@db_session
def delete_calibration_cctv(id: int):
    query = CctvConnection.get(id = id)
    if not query:
        raise HTTPException(status_code=404, detail="Item not found")
    
    query.delete()
    response = get_response_format(200, message = f"CCTV data with id of {id} has been deleted")

    return response 

#-- AUTH endpoints

@app.post("/auth/login", response_model = ResponseAPI, response_model_exclude_none = True)
@db_session
def login(response: Response, req: LoginRequest):
    user = User.get(email=req.email)
    if not user or not user.verify_password(req.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token_data = {"sub": str(user.id), "exp": expire}
    token = jwt.encode(token_data, SECRET_KEY, algorithm=ALGORITHM)

    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        secure=False  # change to True in HTTPS
    )

    response = get_response_format(200, data = token, message = f"Logged in successfully")

    return response

@router.post("/auth/logout", response_model = ResponseAPI, response_model_exclude_none = True)
def logout(response: Response):
    response.delete_cookie(
        key="access_token",
        httponly=True,
        secure=False  # change to True in HTTPS
    )
    
    response = get_response_format(200, message = f"Logged out successfully")

    return response

app.include_router(router)