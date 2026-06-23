import { useEffect, useRef, useState, useCallback } from "react";
import { useAuth } from "@/context/AuthContext";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || "";

/**
 * Live event stream from the backend WebSocket.
 * Auto-reconnects on disconnect. Auth via the access_token cookie (sent automatically
 * by the browser on same-origin upgrade).
 */
export function useLiveStream() {
    const { user } = useAuth();
    const [connected, setConnected] = useState(false);
    const [lastEvent, setLastEvent] = useState(null);
    const wsRef = useRef(null);
    const retryRef = useRef(0);
    const retryTimerRef = useRef(null);
    const closedRef = useRef(false);

    const connect = useCallback(() => {
        if (!user || !user.id || closedRef.current) return;
        const proto = BACKEND_URL.startsWith("https") ? "wss" : "ws";
        const host = BACKEND_URL.replace(/^https?:\/\//, "");
        const url = `${proto}://${host}/api/ws`;
        try {
            const ws = new WebSocket(url);
            wsRef.current = ws;
            ws.onopen = () => { setConnected(true); retryRef.current = 0; };
            ws.onmessage = (e) => {
                try {
                    const msg = JSON.parse(e.data);
                    setLastEvent({ ...msg, _ts: Date.now() });
                } catch (err) {
                    console.warn("[ws] message parse failed", err);
                }
            };
            ws.onclose = () => {
                setConnected(false);
                if (closedRef.current) return;
                const delay = Math.min(15000, 1000 * Math.pow(2, retryRef.current++));
                retryTimerRef.current = setTimeout(connect, delay);
            };
            ws.onerror = (err) => {
                console.warn("[ws] socket error", err?.message || "");
                try { ws.close(); } catch (e2) { console.warn("[ws] close failed", e2); }
            };
        } catch (err) {
            console.warn("[ws] connect failed, will retry", err);
        }
    }, [user]);

    useEffect(() => {
        if (!user || !user.id) return;
        closedRef.current = false;
        connect();
        return () => {
            closedRef.current = true;
            if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
            const ws = wsRef.current;
            if (ws) {
                ws.onclose = null;  // prevent retry storm on intentional teardown
                ws.onerror = null;
                // Only close() when the socket has finished connecting — calling
                // close() on a CONNECTING socket triggers a browser console warning
                // ("WebSocket closed before connection established"). For sockets
                // still mid-handshake, defer close() until onopen fires.
                if (ws.readyState === WebSocket.OPEN) {
                    try { ws.close(); } catch (err) { console.warn("[ws] teardown close failed", err); }
                } else if (ws.readyState === WebSocket.CONNECTING) {
                    ws.onopen = () => { try { ws.close(); } catch { /* noop */ } };
                }
            }
        };
    }, [user, connect]);

    return { lastEvent, connected };
}
