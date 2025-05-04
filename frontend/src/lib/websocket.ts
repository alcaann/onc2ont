// --- START OF UPDATED frontend/src/lib/websocket.ts ---

// Define the structure for the result payload
interface ResultPayload {
    json_data: { // Contains the concepts and relations
        concepts: any[];
        relations: any[];
    };
    raw_xmi: string; // The raw XMI string
}

// Define callback function types
type LogCallback = (message: string) => void;
type ResultCallback = (data: ResultPayload) => void; // Updated type
type ErrorCallback = (error: string) => void;
type StatusCallback = (status: 'connected' | 'disconnected' | 'processing' | 'error') => void;

// Configuration
const RECONNECT_DELAY = 5000; // 5 seconds

// State variables
let websocket: WebSocket | null = null;
let isConnected = false;
let isProcessing = false; // Tracks if the backend is currently working on a request
let reconnectTimeout: NodeJS.Timeout | null = null;

// Callback registries
let logCallbacks: LogCallback[] = [];
let resultCallbacks: ResultCallback[] = []; // Type updated
let errorCallbacks: ErrorCallback[] = [];
let statusCallbacks: StatusCallback[] = [];

// --- Helper Functions ---

function updateStatus(status: 'connected' | 'disconnected' | 'processing' | 'error') {
    console.log(`[websocket.ts] updateStatus called with: ${status}`);

    // Update the internal processing flag based directly on the input status
    if (status === 'processing') {
        isProcessing = true;
    } else if (status === 'connected' || status === 'disconnected' || status === 'error') {
        // Explicitly set isProcessing to false when the task is finished,
        // or if disconnected/error occurs.
        isProcessing = false;
    }

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

    console.log(`WS Status Update: ${displayStatus}`); // Should now correctly reflect the intended state
    console.log(`[websocket.ts] Notifying ${statusCallbacks.length} status listeners with: ${displayStatus}`);
    statusCallbacks.forEach(cb => cb(displayStatus));

    // Also update the global status indicator in the layout (Keep this logic if you use it)
    if (typeof document !== 'undefined') { // Check if running in browser
        const statusElement = document.getElementById('connection-status');
        if (statusElement) {
            statusElement.textContent = displayStatus.charAt(0).toUpperCase() + displayStatus.slice(1);
            statusElement.className = `status-indicator status-${displayStatus}`;
        }
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

    // --- START: Calculate Dynamic WebSocket URL ---
    if (typeof window === 'undefined') {
        console.error("[websocket.ts] Cannot connect: not running in a browser environment.");
        return; // Cannot determine location outside browser
    }
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${window.location.host}/ws`;
    // Example: If browser accesses http://localhost:8080, this becomes ws://localhost:8080/ws
    // Nginx will receive this and proxy it to ws://app:8000/ws
    // --- END: Calculate Dynamic WebSocket URL ---


    console.log(`Attempting to connect to ${wsUrl}...`); // Log the dynamic URL
    try {
        websocket = new WebSocket(wsUrl); // Use the dynamic URL
    } catch (error) {
        console.error("WebSocket constructor failed:", error);
        // Optionally update status to error here
        isConnected = false; // Ensure flags are correct
        isProcessing = false;
        updateStatus('error'); // Indicate connection error
        // Schedule reconnect if constructor fails
        if (!reconnectTimeout) {
           reconnectTimeout = setTimeout(connect, RECONNECT_DELAY);
        }
        return;
    }


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
                    // --- START: Validate and handle new result structure ---
                    if (message.payload && typeof message.payload === 'object' &&
                        message.payload.json_data && typeof message.payload.json_data === 'object' &&
                        Array.isArray(message.payload.json_data.concepts) &&
                        Array.isArray(message.payload.json_data.relations) &&
                        typeof message.payload.raw_xmi === 'string')
                    {
                        console.log(`[websocket.ts] Notifying ${resultCallbacks.length} result listeners.`);
                        // Notify listeners *before* updating status
                        resultCallbacks.forEach(cb => cb(message.payload as ResultPayload)); // Cast to the defined interface
                        console.log("[websocket.ts] Calling updateStatus('connected') after result.");
                        updateStatus('connected'); // Set status back to connected (and isProcessing to false)
                    } else {
                        console.error("[websocket.ts] Received 'result' message with unexpected payload structure:", message.payload);
                        errorCallbacks.forEach(cb => cb("Received invalid result data structure from server."));
                        updateStatus('error');
                    }
                    // --- END: Validate and handle new result structure ---
                    break;
                case 'error':
                    console.log("[websocket.ts] Handling 'error' message.");
                    errorCallbacks.forEach(cb => cb(message.payload));
                    updateStatus('error'); // Show error status (and set isProcessing to false)
                    break;
                // --- START: Handle OWL Result (if needed elsewhere, keep it) ---
                case 'owl_result':
                    console.log("[websocket.ts] Handling 'owl_result' message.");
                    // You might want a dedicated callback for this or handle it differently
                    // For now, just logging it.
                    // owlResultCallbacks.forEach(cb => cb(message.payload));
                    break;
                // --- END: Handle OWL Result ---
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
        // Don't update status here directly, let onclose handle disconnect state
    };

    websocket.onclose = (event) => {
        console.log(`WebSocket connection closed. Code: ${event.code}, Reason: ${event.reason}, Clean: ${event.wasClean}`);
        const wasConnected = isConnected; // Store previous state
        isConnected = false;
        websocket = null;
        // Status update handles setting isProcessing=false and notifying listeners
        updateStatus('disconnected');

        // Attempt to reconnect after a delay only if it wasn't a clean close requested by client
        // and if it was previously connected (to avoid reconnect loops on initial failure)
        if (event.code !== 1000 && wasConnected && !reconnectTimeout) {
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
    } else {
        console.log("Manual disconnect requested, but WebSocket was not active.")
    }
    // Update status explicitly after manual disconnect request
    // Only change flags if they weren't already false
    if (isConnected || isProcessing) {
       isConnected = false;
       isProcessing = false; // Ensure processing stops on manual disconnect
       updateStatus('disconnected');
    }
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
            // Avoid triggering another updateStatus if already disconnected
            if (isConnected) {
                updateStatus('disconnected'); // Update only if it thought it was connected
            }
            // Optionally attempt reconnect if sending fails due to disconnect
            // connect(); // Or use scheduleReconnect logic if preferred
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

    // Subscription methods
    onLog: (callback: LogCallback) => {
        logCallbacks.push(callback);
        return () => { logCallbacks = logCallbacks.filter(cb => cb !== callback); };
    },
    onResult: (callback: ResultCallback) => { // Type updated
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
        let currentDisplayStatus: 'connected' | 'disconnected' | 'processing' | 'error';
        if (!isConnected) {
           currentDisplayStatus = 'disconnected';
        } else if (isProcessing) {
           currentDisplayStatus = 'processing';
        } else {
           currentDisplayStatus = 'connected'; // Assume connected if not disconnected/processing
        }
        // Check if the last known reason for connection loss was an error? (More complex state needed)
        // Simple approach: If not connected, show disconnected. If connected, show processing if active, else connected.
        console.log(`[websocket.ts] Immediately notifying new status listener with: ${currentDisplayStatus}`);
        callback(currentDisplayStatus);
         return () => { statusCallbacks = statusCallbacks.filter(cb => cb !== callback); };
    },

    // Initialize connection when module loads
    initialize: () => {
        if (typeof window !== 'undefined') { // Ensure runs only in browser
            console.log("[websocket.ts] Initializing WebSocket manager.");
            if (!websocket && !reconnectTimeout) { // Prevent multiple initial connections
               connect();
            }
        } else {
            console.log("[websocket.ts] Not in browser environment, skipping WebSocket initialization.");
        }
    }
};

// Auto-initialize when the module is loaded in a browser context
wsManager.initialize();

// --- END OF UPDATED frontend/src/lib/websocket.ts ---