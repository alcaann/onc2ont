# --- START OF FILE api/main.py ---

import asyncio
import json
import logging
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketState
from typing import Optional, List, Dict, Any # Added for clarity
from collections.abc import Callable # Added for clarity


# Adjust the import path based on your project structure
# If main.py is in api/ and process_phrase.py is in scripts/, this should work
# assuming PYTHONPATH includes /app and /app/scripts
try:
    from scripts.process_phrase import process_phrase
except ImportError:
    # Fallback if running locally or PYTHONPATH isn't set as expected in IDE
    import sys
    # Assuming the script is run from the project root or /app in container
    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
    from scripts.process_phrase import process_phrase


# Configure logging for the API
log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)
# Configure root logger level for FastAPI/Uvicorn logs
logging.basicConfig(level=log_level, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__) # Logger specific to this API file

app = FastAPI()

# WebSocket Message Format Documentation (for clarity)
# Client -> Server: {'type': 'process_phrase', 'payload': 'text phrase'}
# Server -> Client: {'type': 'log', 'payload': 'log message'}
# Server -> Client: {'type': 'result', 'payload': {'concepts': [...], 'relations': [...]}}
# Server -> Client: {'type': 'error', 'payload': 'error message'}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handles WebSocket connections for phrase processing."""
    await websocket.accept()
    logger.info(f"WebSocket connection accepted from: {websocket.client.host}:{websocket.client.port}")

    log_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    log_sender_task = None
    main_event_loop = asyncio.get_running_loop() # Capture the main event loop

    async def send_logs():
        """Reads logs from the queue and sends them over WebSocket."""
        while True:
            try:
                message = await log_queue.get()
                if message is None: # Sentinel value to stop the task
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
                    break # Exit if websocket closed
                log_queue.task_done()
            except Exception as e:
                 logger.error(f"Error in send_logs task: {e}", exc_info=True)
                 try: log_queue.task_done()
                 except ValueError: pass
                 break # Stop the task on error

    try:
        while True:
            # Wait for a message from the client
            data = await websocket.receive_text()
            logger.info(f"Received message via WS: {data}")

            try:
                message_data = json.loads(data)
                msg_type = message_data.get("type")
                payload = message_data.get("payload")

                if msg_type == "process_phrase" and isinstance(payload, str):
                    phrase = payload
                    logger.info(f"Processing request for phrase: '{phrase}'")

                    # Clean up previous task if still running
                    if log_sender_task and not log_sender_task.done():
                         logger.warning("New request received before previous log sender finished. Attempting cleanup.")
                         try:
                              await log_queue.put(None) # Signal old sender to stop
                              await asyncio.wait_for(log_sender_task, timeout=1.0) # Wait briefly
                         except asyncio.TimeoutError:
                              logger.warning("Timeout waiting for previous log sender task to stop.")
                         except Exception as e:
                              logger.error(f"Error cleaning up previous log sender: {e}")

                    # Clear the queue for the new request
                    while not log_queue.empty():
                         try:
                              log_queue.get_nowait()
                              log_queue.task_done()
                         except asyncio.QueueEmpty: break
                         except ValueError: pass

                    # Start the log sending task for this request
                    log_sender_task = asyncio.create_task(send_logs())

                    # Define the synchronous callback function to put logs onto the async queue
                    def log_callback_sync(message: str):
                        try:
                            # Use the captured main_event_loop for thread safety
                            main_event_loop.call_soon_threadsafe(log_queue.put_nowait, message)
                        except Exception as e:
                            # Log error if putting to queue fails (e.g., queue full, loop stopped)
                            logger.error(f"Error submitting log message to queue from thread: {e}")


                    # Run the potentially long-running, synchronous process_phrase in a separate thread
                    try:
                        results = await asyncio.to_thread(
                            process_phrase,
                            phrase,
                            log_func=log_callback_sync # Use correct keyword arg name 'log_func'
                        )
                        logger.info(f"Processing completed for phrase: '{phrase}'. Found {len(results.get('concepts',[]))} concepts, {len(results.get('relations',[]))} relations.")

                        # Send the final results if client still connected
                        if websocket.client_state == WebSocketState.CONNECTED:
                            result_payload = json.dumps({"type": "result", "payload": results})
                            await websocket.send_text(result_payload)
                            logger.debug("Sent final results via WS.")
                        else:
                            logger.warning("WebSocket disconnected before final results could be sent.")

                    except Exception as e:
                        logger.error(f"Error during process_phrase execution for '{phrase}': {e}", exc_info=True)
                        # Send error message to client if connected
                        if websocket.client_state == WebSocketState.CONNECTED:
                            error_payload = json.dumps({"type": "error", "payload": f"Processing failed: {e}"})
                            await websocket.send_text(error_payload)
                    finally:
                        # Signal the log sender task to finish and wait for it
                        if log_sender_task and not log_sender_task.done():
                            logger.debug("Signalling log sender task to stop.")
                            await log_queue.put(None) # Send sentinel
                            try:
                                await asyncio.wait_for(log_sender_task, timeout=5.0)
                                logger.debug("Log sender task finished.")
                            except asyncio.TimeoutError:
                                logger.warning("Timeout waiting for log sender task to finish after processing.")
                            except Exception as e:
                                logger.error(f"Error waiting for log sender task: {e}")

                else:
                    logger.warning(f"Received invalid message format via WS: {data}")
                    if websocket.client_state == WebSocketState.CONNECTED:
                         error_payload = json.dumps({"type": "error", "payload": "Invalid message format. Send {'type': 'process_phrase', 'payload': 'your text'}"})
                         await websocket.send_text(error_payload)

            except json.JSONDecodeError:
                logger.warning(f"Received non-JSON message via WS: {data}")
                if websocket.client_state == WebSocketState.CONNECTED:
                    error_payload = json.dumps({"type": "error", "payload": "Invalid JSON message received."})
                    await websocket.send_text(error_payload)
            except Exception as e:
                 logger.error(f"Unexpected error handling WebSocket message: {e}", exc_info=True)
                 if websocket.client_state == WebSocketState.CONNECTED:
                     try:
                          error_payload = json.dumps({"type": "error", "payload": f"Internal server error: {e}"})
                          await websocket.send_text(error_payload)
                     except Exception as send_err:
                          logger.error(f"Failed to send error message over WebSocket: {send_err}")


    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {websocket.client.host}:{websocket.client.port}")
        # Signal and wait for log sender task to stop if it's running
        if log_sender_task and not log_sender_task.done():
             logger.debug("Client disconnected. Signalling log sender task to stop.")
             await log_queue.put(None) # Send sentinel
             try:
                  await asyncio.wait_for(log_sender_task, timeout=1.0) # Brief wait
             except asyncio.TimeoutError:
                  logger.warning("Timeout waiting for log sender task to finish after disconnect.")
             except Exception as e:
                 logger.error(f"Error cleaning up log sender task after disconnect: {e}")

    except Exception as e:
        logger.error(f"Unexpected error in WebSocket endpoint: {e}", exc_info=True)
        # Ensure connection is closed if possible on unexpected errors
        if websocket.client_state == WebSocketState.CONNECTED:
             await websocket.close(code=1011) # Internal Error code

    finally:
        logger.info(f"WebSocket connection closed for: {websocket.client.host}:{websocket.client.port}")


# Optional: Add a root endpoint for basic check or serving HTML
@app.get("/", response_class=HTMLResponse)
async def read_root():
    # In a real app, you'd serve your built SPA index.html here
    # For now, just a confirmation message
    return """
    <html>
        <head><title>Oncology Phrase Processor API</title></head>
        <body>
            <h1>Oncology Phrase Processor API</h1>
            <p>Connect via WebSocket at <code>/ws</code>.</p>
            <p>Send JSON: <code>{"type": "process_phrase", "payload": "your oncology phrase"}</code></p>
            <p>Receive JSON: <code>{"type": "log", "payload": "..."}</code> or <code>{"type": "result", "payload": {...}}</code> or <code>{"type": "error", "payload": "..."}</code></p>
        </body>
    </html>
    """

# --- END OF FILE api/main.py ---