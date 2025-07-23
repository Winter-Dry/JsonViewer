import os
import uuid
import json
import threading
import time
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key='your_secret_key')

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

templates = Jinja2Templates(directory="templates")


metadata_lock = threading.Lock()
file_metadata = {} # file_id -> {'filename': str, 'last_access': datetime}

# Global cache：file_id -> line index
line_index_cache = {}

def cleanup_old_files(hour_thr=10):
    """
    clean outdated files from the upload folder.
    """
    print(f"[{datetime.now()}] Running cleanup task...")
    expiration_time = datetime.now() - timedelta(hours=hour_thr)
    files_to_delete = []
    with metadata_lock:
        for file_id, metadata in file_metadata.items():
            if metadata['last_access'] < expiration_time:
                files_to_delete.append(file_id)
        for file_id in files_to_delete:
            filename = file_metadata[file_id]['filename']
            filepath = os.path.join(UPLOAD_FOLDER, f"{file_id}_{filename}")
            print(f"Deleting expired file: {filepath}")
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError as e:
                    print(f"Error removing file {filepath}: {e}")
            if file_id in file_metadata:
                del file_metadata[file_id]
            if file_id in line_index_cache:
                del line_index_cache[file_id]
    print(f"[{datetime.now()}] Cleanup task finished.")
def run_cleanup_scheduler():
    while True:
        cleanup_old_files()
        time.sleep(3600)

def get_user_file(request: Request):
    file_id = request.session.get('file_id')
    filename = request.session.get('filename')
    if not file_id or not filename:
        return None, None
    filepath = os.path.join(UPLOAD_FOLDER, f"{file_id}_{filename}")
    return filepath, file_id

def build_line_index(filepath):
    index = []
    offset = 0
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            index.append(offset)
            offset += len(line.encode('utf-8'))
    return index

def read_line_by_index(filepath, index, line_no):
    with open(filepath, "r", encoding="utf-8") as f:
        f.seek(index[line_no])
        return f.readline()

# --- FastAPI 应用启动事件 ---
@app.on_event("startup")
async def startup_event():
    cleanup_thread = threading.Thread(target=run_cleanup_scheduler, daemon=True)
    cleanup_thread.start()
    print("Cleanup scheduler started in the background.")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "error": None})

@app.post("/", response_class=HTMLResponse)
async def upload_file(request: Request, file: UploadFile = File(...)):
    contents = await file.read()
    file_id = str(uuid.uuid4())
    filename = file.filename
    filepath = os.path.join(UPLOAD_FOLDER, f"{file_id}_{filename}")
    with open(filepath, "wb") as f:
        f.write(contents)

    request.session['file_id'] = file_id
    request.session['filename'] = filename
    with metadata_lock:
        file_metadata[file_id] = {
            'filename': filename,
            'last_access': datetime.now()
        }
        if file_id in line_index_cache:
            del line_index_cache[file_id]
    return RedirectResponse(url="/viewer?page=1", status_code=303)

@app.get("/viewer", response_class=HTMLResponse)
async def viewer(request: Request, page: int = 1):
    filepath, file_id = get_user_file(request)
    if not filepath or not os.path.exists(filepath):
        return RedirectResponse(url="/", status_code=303)
    with metadata_lock:
        metadata = file_metadata.get(file_id)
        if not metadata:
            return RedirectResponse(url="/", status_code=303)
        metadata['last_access'] = datetime.now()

    with metadata_lock:
        if file_id not in line_index_cache:
            line_index_cache[file_id] = build_line_index(filepath)

    index = line_index_cache[file_id]
    total = len(index)
    if total == 0:
        return RedirectResponse(url="/", status_code=303)
    if page < 1:
        page = 1
    if page > total:
        page = total
    line = read_line_by_index(filepath, index, page - 1)
    try:
        data = json.loads(line)
    except Exception as e:
        data = {'error': f'Json load failed: {e}', 'Original content': line}
    return templates.TemplateResponse("viewer.html", {
        "request": request,
        "data": json.dumps(data, ensure_ascii=False, indent=4),
        "page": page,
        "total": total
    })
