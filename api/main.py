# --- START OF REVERTED api/main.py ---

import asyncio
import json
import logging
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect # Removed Request
# Removed FileResponse, StaticFiles
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketState
from typing import Optional, List, Dict, Any
from collections.abc import Callable


# Adjust the import path based on your project structure
try:
    from scripts.process_phrase import process_phrase
except ImportError:
    import sys
    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
    from scripts.process_phrase import process_phrase


# Configure logging for the API
log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)
logging.basicConfig(level=log_level, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()

# WebSocket Message Format Documentation (Keep this)
# ...

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # --- WebSocket logic remains unchanged ---
    # (Keep the entire content of your original websocket_endpoint function here)
    # ... (full implementation as you provided before) ...
    await websocket.accept()
    logger.info(f"WebSocket connection accepted from: {websocket.client.host}:{websocket.client.port}")
    log_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    log_sender_task = None
    main_event_loop = asyncio.get_running_loop()

    async def send_logs():
        # (Keep original send_logs implementation)
        while True:
            try:
                message = await log_queue.get()
                if message is None:
                    logger.debug("Log sending task received None sentinel. Stopping.")
                    log_queue.task_done()
                    break
                if websocket.client_state == WebSocketState.CONNECTED:
                    log_payload = json.dumps({"type": "log", "payload": message})
                    await websocket.send_text(log_payload)
                    logger.debug(f"Sent log via WS: {message}")
                else:
                    logger.warning("WebSocket disconnected while trying to send log.")
                    log_queue.task_done()
                    break
                log_queue.task_done()
            except Exception as e:
                 logger.error(f"Error in send_logs task: {e}", exc_info=True)
                 try: log_queue.task_done()
                 except ValueError: pass
                 break

    try:
        while True:
            data = await websocket.receive_text()
            logger.info(f"Received message via WS: {data}")
            # (Keep original message handling try/except block)
            try:
                message_data = json.loads(data)
                msg_type = message_data.get("type")
                payload = message_data.get("payload")

                if msg_type == "process_phrase" and isinstance(payload, str):
                    phrase = payload
                    logger.info(f"Processing request for phrase: '{phrase}'")

                    # Cleanup previous task if necessary
                    if log_sender_task and not log_sender_task.done():
                         logger.warning("Cleaning up previous log sender task.")
                         try:
                              # Use put_nowait in threadsafe context isn't ideal, but might work here
                              # Better: cancel the task directly if possible
                              log_sender_task.cancel()
                         except Exception as e:
                             logger.error(f"Error cancelling previous task: {e}")

                    # Clear the queue for the new request
                    while not log_queue.empty():
                         try: log_queue.get_nowait(); log_queue.task_done()
                         except (asyncio.QueueEmpty, ValueError): break

                    # Start the log sending task for this request
                    log_sender_task = asyncio.create_task(send_logs())

                    # Define the synchronous callback function
                    def log_callback_sync(message: str):
                        try:
                            main_event_loop.call_soon_threadsafe(log_queue.put_nowait, message)
                        except Exception as e:
                            logger.error(f"Error submitting log message to queue from thread: {e}")

                    # Run processing in thread
                    try:
                        results = await asyncio.to_thread(
                            process_phrase,
                            phrase,
                            log_func=log_callback_sync
                        )
                        logger.info(f"Processing completed for phrase: '{phrase}'. Found {len(results.get('concepts',[]))} concepts, {len(results.get('relations',[]))} relations.")

                        if websocket.client_state == WebSocketState.CONNECTED:
                            result_payload = json.dumps({"type": "result", "payload": results})
                            await websocket.send_text(result_payload)
                            logger.debug("Sent final results via WS.")
                        else:
                            logger.warning("WebSocket disconnected before final results could be sent.")

                    except Exception as e:
                        logger.error(f"Error during process_phrase execution for '{phrase}': {e}", exc_info=True)
                        if websocket.client_state == WebSocketState.CONNECTED:
                            error_payload = json.dumps({"type": "error", "payload": f"Processing failed: {e}"})
                            await websocket.send_text(error_payload)
                    finally:
                        # Signal and wait for log sender task
                        if log_sender_task and not log_sender_task.done():
                            logger.debug("Signalling log sender task to stop.")
                            # Put sentinel only if task wasn't cancelled earlier
                            if not log_sender_task.cancelled():
                                await log_queue.put(None)
                            try:
                                await asyncio.wait_for(log_sender_task, timeout=5.0)
                                logger.debug("Log sender task finished.")
                            except (asyncio.TimeoutError, asyncio.CancelledError):
                                logger.warning("Log sender task timed out or was cancelled.")
                            except Exception as e:
                                logger.error(f"Error waiting for log sender task: {e}")
                else:
                    # Handle invalid message format (keep original code)
                    logger.warning(f"Received invalid message format via WS: {data}")
                    if websocket.client_state == WebSocketState.CONNECTED:
                         error_payload = json.dumps({"type": "error", "payload": "Invalid message format."})
                         await websocket.send_text(error_payload)

            except json.JSONDecodeError:
                # Handle non-JSON message (keep original code)
                 logger.warning(f"Received non-JSON message via WS: {data}")
                 if websocket.client_state == WebSocketState.CONNECTED:
                    error_payload = json.dumps({"type": "error", "payload": "Invalid JSON message received."})
                    await websocket.send_text(error_payload)
            except Exception as e:
                 # Handle unexpected errors (keep original code)
                 logger.error(f"Unexpected error handling WebSocket message: {e}", exc_info=True)
                 if websocket.client_state == WebSocketState.CONNECTED:
                     try:
                          error_payload = json.dumps({"type": "error", "payload": f"Internal server error: {e}"})
                          await websocket.send_text(error_payload)
                     except Exception as send_err:
                          logger.error(f"Failed to send error message over WebSocket: {send_err}")

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {websocket.client.host}:{websocket.client.port}")
        # Signal and clean up log sender task (keep original code)
        if log_sender_task and not log_sender_task.done():
             logger.debug("Client disconnected. Cancelling log sender task.")
             log_sender_task.cancel()
             # Give it a moment to clean up if needed, but don't wait indefinitely
             # await asyncio.sleep(0.1)

    except Exception as e:
        logger.error(f"Unexpected error in WebSocket endpoint: {e}", exc_info=True)
        if websocket.client_state == WebSocketState.CONNECTED:
             await websocket.close(code=1011)

    finally:
        logger.info(f"WebSocket connection closed for: {websocket.client.host}:{websocket.client.port}")
        # Ensure task is cancelled on final exit
        if log_sender_task and not log_sender_task.done():
            log_sender_task.cancel()


# --- REMOVED Static File Mounts and Catch-all Route ---

# Optional: Add back a simple root endpoint for basic health check if desired
@app.get("/", response_class=HTMLResponse)
async def read_root():
    return """
    <html><head><title>Oncology Processor API</title></head>
    <body><h1>Backend API is Running</h1><p>Connect via WebSocket at /ws (through proxy).</p></body>
    </html>
    """

# --- END OF REVERTED api/main.py ---