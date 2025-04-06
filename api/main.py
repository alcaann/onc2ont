# --- START OF CORRECTED api/main.py ---

import asyncio
import json
import logging
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketState
from typing import Optional, List, Dict, Any
from collections.abc import Callable

# --- Pipeline Imports ---
# Import the base class and the specific implementation
try:
    from pipelines.base_pipeline import BaseProcessingPipeline
    from pipelines.spacy_rule_based.processor import SpacyRuleBasedPipeline
    # Example: Import other pipelines here if you create them
    # from pipelines.other_model.processor import OtherModelPipeline
except ImportError as e:
    logging.error(f"FATAL: Failed to import pipeline classes: {e}", exc_info=True)
    # Define dummy classes to prevent NameErrors if import fails, app should ideally not start
    class BaseProcessingPipeline: pass
    class SpacyRuleBasedPipeline(BaseProcessingPipeline):
        def __init__(self, *args, **kwargs): logging.error("Dummy SpacyRuleBasedPipeline initialized due to import error."); self.nlp = None
        def process(self, *args, **kwargs): logging.error("Dummy process called."); return {'concepts': [], 'relations': []}


# Configure logging for the API
log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)
logging.basicConfig(level=log_level, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__) # Logger specific to this API file

# --- Instantiate the Processing Pipeline ---
# TODO: Make pipeline selection configurable (e.g., via environment variable)
PIPELINE_TO_USE = "spacy_rule_based" # Or load from os.getenv("PROCESSING_PIPELINE", "spacy_rule_based")

processing_pipeline: Optional[BaseProcessingPipeline] = None
try:
    logger.info(f"Attempting to initialize pipeline: {PIPELINE_TO_USE}")
    if PIPELINE_TO_USE == "spacy_rule_based":
        # Pass optional config dict here if needed: SpacyRuleBasedPipeline(config={...})
        processing_pipeline = SpacyRuleBasedPipeline()
    # elif PIPELINE_TO_USE == "other_model":
    #     processing_pipeline = OtherModelPipeline()
    else:
        logger.error(f"Unknown processing pipeline specified: {PIPELINE_TO_USE}")

    if processing_pipeline is None:
         raise RuntimeError(f"Pipeline '{PIPELINE_TO_USE}' could not be instantiated.")
    # Check if the instantiated pipeline seems functional (e.g., model loaded)
    # This check is specific to the Spacy pipeline for now
    if isinstance(processing_pipeline, SpacyRuleBasedPipeline) and not processing_pipeline.nlp:
         raise RuntimeError(f"Pipeline '{PIPELINE_TO_USE}' initialized but failed to load its NLP model.")

    logger.info(f"Successfully initialized pipeline: {processing_pipeline.__class__.__name__}")

except Exception as e:
    logger.error(f"FATAL: Failed to initialize processing pipeline '{PIPELINE_TO_USE}': {e}", exc_info=True)
    processing_pipeline = None # Ensure it's None if initialization fails

app = FastAPI()

# WebSocket Message Format Documentation (Keep this)
# Client -> Server: {'type': 'process_phrase', 'payload': 'text phrase'}
# Server -> Client: {'type': 'log', 'payload': 'log message'}
# Server -> Client: {'type': 'result', 'payload': {'concepts': [...], 'relations': [...]}}
# Server -> Client: {'type': 'error', 'payload': 'error message'}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # --- Check if pipeline is available before accepting ---
    if processing_pipeline is None:
        logger.error("Pipeline not available, rejecting WebSocket connection.")
        # Don't accept the connection, let it close. Or send an error and close.
        await websocket.close(code=1011, reason="Pipeline initialization failed")
        return

    await websocket.accept()
    logger.info(f"WebSocket connection accepted from: {websocket.client.host}:{websocket.client.port}")
    log_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    log_sender_task = None
    main_event_loop = asyncio.get_running_loop()

    async def send_logs():
        # (Implementation remains the same)
        while True:
            try:
                message = await log_queue.get()
                if message is None: # Sentinel value to stop the task
                    # logger.debug("Log sending task received None sentinel. Stopping.") # Reduce verbosity
                    log_queue.task_done(); break
                if websocket.client_state == WebSocketState.CONNECTED:
                    log_payload = json.dumps({"type": "log", "payload": message})
                    await websocket.send_text(log_payload)
                    # logger.debug(f"Sent log via WS: {message}") # Reduce verbosity
                else:
                    logger.warning("WebSocket disconnected while trying to send log.")
                    log_queue.task_done(); break # Exit if websocket closed
                log_queue.task_done()
            except Exception as e:
                 logger.error(f"Error in send_logs task: {e}", exc_info=True)
                 try: log_queue.task_done()
                 except ValueError: pass
                 break # Stop the task on error

    try:
        while True:
            data = await websocket.receive_text()
            # logger.info(f"Received message via WS: {data}") # Reduce verbosity

            try:
                message_data = json.loads(data)
                msg_type = message_data.get("type")
                payload = message_data.get("payload")

                if msg_type == "process_phrase" and isinstance(payload, str):
                    phrase = payload
                    logger.info(f"Processing request for phrase: '{phrase}' using {processing_pipeline.__class__.__name__}")

                    # Cleanup previous task if necessary
                    if log_sender_task and not log_sender_task.done():
                         logger.warning("Cancelling previous log sender task for new request.")
                         log_sender_task.cancel() # Request cancellation

                    # Clear the queue for the new request
                    while not log_queue.empty():
                         try: log_queue.get_nowait(); log_queue.task_done()
                         except (asyncio.QueueEmpty, ValueError): break

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


                    # --- Execute the pipeline's process method in a thread ---
                    try:
                        # Ensure processing_pipeline is not None (checked at start, but good practice)
                        if processing_pipeline is None:
                            raise RuntimeError("Processing pipeline is not available.")

                        results = await asyncio.to_thread(
                            processing_pipeline.process, # Call the 'process' method of the INSTANCE
                            phrase,
                            log_func=log_callback_sync # Pass the callback function
                        )
                        logger.info(f"Processing completed for phrase: '{phrase}'. Found {len(results.get('concepts',[]))} concepts, {len(results.get('relations',[]))} relations.")

                        if websocket.client_state == WebSocketState.CONNECTED:
                            result_payload = json.dumps({"type": "result", "payload": results})
                            await websocket.send_text(result_payload)
                            logger.debug("Sent final results via WS.")
                        else:
                            logger.warning("WebSocket disconnected before final results could be sent.")

                    except Exception as e:
                        # Catch errors from the pipeline.process call
                        logger.error(f"Error during pipeline execution for '{phrase}': {e}", exc_info=True)
                        if websocket.client_state == WebSocketState.CONNECTED:
                            error_payload = json.dumps({"type": "error", "payload": f"Processing failed: {e}"})
                            await websocket.send_text(error_payload)
                    finally:
                        # Signal and wait for log sender task to finish
                        if log_sender_task and not log_sender_task.done():
                            logger.debug("Signalling log sender task to stop.")
                            # Put sentinel only if task wasn't cancelled earlier
                            if not log_sender_task.cancelled():
                                await log_queue.put(None) # Send sentinel
                            try:
                                await asyncio.wait_for(log_sender_task, timeout=5.0) # Wait for logs to flush
                                logger.debug("Log sender task finished.")
                            except (asyncio.TimeoutError, asyncio.CancelledError):
                                logger.warning("Log sender task timed out or was cancelled.")
                            except Exception as e:
                                logger.error(f"Error waiting for log sender task: {e}")
                else:
                    # Handle invalid message format
                     logger.warning(f"Received invalid message format via WS: {data}")
                     if websocket.client_state == WebSocketState.CONNECTED:
                          error_payload = json.dumps({"type": "error", "payload": "Invalid message format. Send {'type': 'process_phrase', 'payload': 'your text'}"})
                          await websocket.send_text(error_payload)

            except json.JSONDecodeError:
                 # Handle non-JSON message
                 logger.warning(f"Received non-JSON message via WS: {data}")
                 if websocket.client_state == WebSocketState.CONNECTED:
                    error_payload = json.dumps({"type": "error", "payload": "Invalid JSON message received."})
                    await websocket.send_text(error_payload)
            except Exception as e:
                 # Handle unexpected errors
                 logger.error(f"Unexpected error handling WebSocket message: {e}", exc_info=True)
                 if websocket.client_state == WebSocketState.CONNECTED:
                     try:
                          error_payload = json.dumps({"type": "error", "payload": f"Internal server error: {e}"})
                          await websocket.send_text(error_payload)
                     except Exception as send_err:
                          logger.error(f"Failed to send error message over WebSocket: {send_err}")

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {websocket.client.host}:{websocket.client.port}")
        # Signal and clean up log sender task
        if log_sender_task and not log_sender_task.done():
             logger.debug("Client disconnected. Cancelling log sender task.")
             log_sender_task.cancel()

    except Exception as e:
        logger.error(f"Unexpected error in WebSocket endpoint: {e}", exc_info=True)
        # Ensure connection is closed if possible on unexpected errors
        if websocket.client_state == WebSocketState.CONNECTED:
             await websocket.close(code=1011) # Internal Error code

    finally:
        logger.info(f"WebSocket connection closed for: {websocket.client.host}:{websocket.client.port}")
        # Ensure task is cancelled on final exit
        if log_sender_task and not log_sender_task.done():
            log_sender_task.cancel()


# Optional: Basic health check endpoint
@app.get("/", response_class=HTMLResponse)
async def read_root():
    # Check pipeline status for health check
    pipeline_status = "Unknown"
    pipeline_name = "N/A"
    if processing_pipeline is not None:
        pipeline_name = processing_pipeline.__class__.__name__
        # Add more specific checks if needed (e.g., processing_pipeline.is_ready())
        # Check the NLP model specific to SpacyRuleBasedPipeline as an example
        if isinstance(processing_pipeline, SpacyRuleBasedPipeline) and processing_pipeline.nlp:
             pipeline_status = f"Pipeline '{pipeline_name}' is loaded and NLP model ready."
        elif isinstance(processing_pipeline, SpacyRuleBasedPipeline) and not processing_pipeline.nlp:
             pipeline_status = f"Pipeline '{pipeline_name}' loaded BUT NLP model FAILED to load."
        else:
             pipeline_status = f"Pipeline '{pipeline_name}' is loaded (basic check)." # For other pipeline types
    else:
        pipeline_status = "Backend API is Running, but Processing Pipeline FAILED to initialize."

    return f"""
    <html><head><title>Oncology Processor API</title></head>
    <body><h1>{pipeline_status}</h1><p>Connect via WebSocket at /ws (through proxy).</p></body>
    </html>
    """

# --- END OF CORRECTED api/main.py ---