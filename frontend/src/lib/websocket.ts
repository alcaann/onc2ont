// frontend/src/lib/websocket.ts (CORRECTED)

// Define callback function types
type LogCallback = (message: string) => void;
type ResultCallback = (data: { concepts: any[]; relations: any[] }) => void;
type ErrorCallback = (error: string) => void;
type StatusCallback = (status: 'connected' | 'disconnected' | 'processing' | 'error') => void;

// Configuration
const WEBSOCKET_URL = "ws://localhost:8000/ws"; // Your FastAPI backend WebSocket URL
const RECONNECT_DELAY = 5000; // 5 seconds

// State variables
let websocket: WebSocket | null = null;
let isConnected = false;
let isProcessing = false; // Tracks if the backend is currently working on a request
let reconnectTimeout: NodeJS.Timeout | null = null;

// Callback registries
let logCallbacks: LogCallback[] = [];
let resultCallbacks: ResultCallback[] = [];
let errorCallbacks: ErrorCallback[] = [];
let statusCallbacks: StatusCallback[] = [];

// --- Helper Functions ---

function updateStatus(status: 'connected' | 'disconnected' | 'processing' | 'error') {
    console.log(`[websocket.ts] updateStatus called with: ${status}`);

    // --- CORRECTED LOGIC for isProcessing flag ---
    // Update the internal processing flag based directly on the input status
    if (status === 'processing') {
        isProcessing = true;
    } else if (status === 'connected' || status === 'disconnected' || status === 'error') {
        // Explicitly set isProcessing to false when the task is finished,
        // or if disconnected/error occurs.
        isProcessing = false;
    }
    // --- END CORRECTED LOGIC ---

    // Determine the final display status based on connection and the *updated* processing state
    let displayStatus: 'connected' | 'disconnected' | 'processing' | 'error';
    if (!isConnected) {
        displayStatus = 'disconnected'; // Highest priority: if not connected, show disconnected
    } else if (isProcessing) {
        // If connected AND the last update indicated processing should start
        displayStatus = 'processing';
    } else {
        // If connected and not processing (implies status was 'connected', 'error', or initial connect)
        displayStatus = 'connected';
    }
    // If an explicit error occurred *during this specific update*, override display status
    // This ensures 'error' state is shown even if technically still connected.
    if (status === 'error') {
        displayStatus = 'error';
    }
    // --- END DISPLAY STATUS CALCULATION ---


    console.log(`WS Status Update: ${displayStatus}`); // Should now correctly reflect the intended state
    console.log(`[websocket.ts] Notifying ${statusCallbacks.length} status listeners with: ${displayStatus}`);
    statusCallbacks.forEach(cb => cb(displayStatus));

    // Also update the global status indicator in the layout
    const statusElement = document.getElementById('connection-status');
    if (statusElement) {
        statusElement.textContent = displayStatus.charAt(0).toUpperCase() + displayStatus.slice(1);
        statusElement.className = `status-indicator status-${displayStatus}`;
    }
}


function connect() {
    if (websocket && websocket.readyState === WebSocket.OPEN) {
        console.log("WebSocket already open.");
        return;
    }
    if (websocket && websocket.readyState === WebSocket.CONNECTING) {
         console.log("WebSocket already connecting.");
         return;
    }

    console.log(`Attempting to connect to ${WEBSOCKET_URL}...`);
    websocket = new WebSocket(WEBSOCKET_URL);

    websocket.onopen = () => {
        console.log("WebSocket connection established.");
        isConnected = true;
        updateStatus('connected'); // Set initial status to connected (and isProcessing to false)
        if (reconnectTimeout) {
            clearTimeout(reconnectTimeout);
            reconnectTimeout = null;
        }
    };

    websocket.onmessage = (event) => {
        try {
            const message = JSON.parse(event.data);
            console.log("[websocket.ts] Raw message received:", event.data);
            console.log("[websocket.ts] Parsed message:", message);

            switch (message.type) {
                case 'log':
                    console.log("[websocket.ts] Handling 'log' message.");
                    logCallbacks.forEach(cb => cb(message.payload));
                    break;
                case 'result':
                    console.log("[websocket.ts] Handling 'result' message. Payload:", message.payload);
                    console.log(`[websocket.ts] Notifying ${resultCallbacks.length} result listeners.`);
                    // Notify listeners *before* updating status
                    resultCallbacks.forEach(cb => cb(message.payload));
                    console.log("[websocket.ts] Calling updateStatus('connected') after result.");
                    updateStatus('connected'); // Set status back to connected (and isProcessing to false)
                    break;
                case 'error':
                    console.log("[websocket.ts] Handling 'error' message.");
                    errorCallbacks.forEach(cb => cb(message.payload));
                    updateStatus('error'); // Show error status (and set isProcessing to false)
                    break;
                default:
                    console.warn("Received unknown message type:", message.type);
            }
        } catch (error) {
            console.error("Failed to parse WebSocket message or handle it:", error);
            errorCallbacks.forEach(cb => cb("Received invalid message from server."));
             updateStatus('error');
        }
    };

    websocket.onerror = (error) => {
        console.error("WebSocket error:", error);
        // Note: onclose usually follows onerror, so status update handled there
        errorCallbacks.forEach(cb => cb("WebSocket connection error."));
    };

    websocket.onclose = (event) => {
        console.log(`WebSocket connection closed. Code: ${event.code}, Reason: ${event.reason}`);
        isConnected = false;
        websocket = null;
        // Status update handles setting isProcessing=false and notifying listeners
        updateStatus('disconnected');
        // Attempt to reconnect after a delay
        if (event.code !== 1000 && !reconnectTimeout) { // Avoid reconnect on clean close (1000)
            console.log(`Attempting to reconnect in ${RECONNECT_DELAY / 1000} seconds...`);
            reconnectTimeout = setTimeout(connect, RECONNECT_DELAY);
        }
    };
}

function disconnect() {
    if (reconnectTimeout) {
       clearTimeout(reconnectTimeout);
       reconnectTimeout = null;
       console.log("Cancelled reconnect attempt.");
    }
    if (websocket) {
        console.log("Closing WebSocket connection manually.");
        websocket.close(1000, "Client disconnecting"); // Clean closure code 1000
        websocket = null; // Prevent further actions
    }
    // Update status explicitly after manual disconnect request
    isConnected = false;
    updateStatus('disconnected');
}

// --- Public API ---

export const wsManager = {
    connect,
    disconnect,

    sendMessage: (type: string, payload: any) => {
        if (!websocket || websocket.readyState !== WebSocket.OPEN) {
            console.error("WebSocket is not connected. Cannot send message.");
            errorCallbacks.forEach(cb => cb("Cannot send message: Not connected."));
            // Ensure status reflects reality if trying to send while disconnected
            if (!isConnected) updateStatus('disconnected');
            return;
        }
        try {
            const message = JSON.stringify({ type, payload });
            console.log("[websocket.ts] Sending message:", message);
            websocket.send(message);
            if (type === 'process_phrase') {
                 console.log("[websocket.ts] Calling updateStatus('processing') after sending request.");
                 // This correctly sets isProcessing=true and notifies listeners
                updateStatus('processing');
            }
        } catch (error) {
            console.error("Failed to stringify or send WebSocket message:", error);
             errorCallbacks.forEach(cb => cb("Failed to send message."));
             updateStatus('error'); // Indicate error state
        }
    },

    onLog: (callback: LogCallback) => {
        logCallbacks.push(callback);
        return () => { logCallbacks = logCallbacks.filter(cb => cb !== callback); };
    },

    onResult: (callback: ResultCallback) => {
        resultCallbacks.push(callback);
         return () => { resultCallbacks = resultCallbacks.filter(cb => cb !== callback); };
    },

    onError: (callback: ErrorCallback) => {
        errorCallbacks.push(callback);
         return () => { errorCallbacks = errorCallbacks.filter(cb => cb !== callback); };
    },

     onStatusChange: (callback: StatusCallback) => {
        statusCallbacks.push(callback);
        // Immediately call with current status derived from flags
        const currentDisplayStatus = isConnected ? (isProcessing ? 'processing' : 'connected') : 'disconnected';
        console.log(`[websocket.ts] Immediately notifying new status listener with: ${currentDisplayStatus}`);
        callback(currentDisplayStatus);
         return () => { statusCallbacks = statusCallbacks.filter(cb => cb !== callback); };
    },

    initialize: () => {
        if (typeof window !== 'undefined') { // Ensure runs only in browser
            if (!websocket && !reconnectTimeout) { // Prevent multiple initial connections
               connect();
            }
        }
    }
};

wsManager.initialize();