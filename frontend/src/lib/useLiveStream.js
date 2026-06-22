import { useEffect, useRef, useState, useCallback } from "react";
import { useAuth } from "@/context/AuthContext";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || "";

/**
 * Live event stream from the backend WebSocket.
 * Auto-reconnects on disconnect. Auth via the access_token cookie (sent automatically
 * by the browser on same-origin upgrade).
 *
 * Usage:
 *   const { lastEvent, connected } = useLiveStream();
 *   useEffect(() => { if (lastEvent?.type === 'trade_updated') ... }, [lastEvent]);
 */
export function useLiveStream() {
    const { user } = useAuth();
    const [connected, setConnected] = useState(false);
    const [lastEvent, setLastEvent] = useState(null);
    const wsRef = useRef(null);
    const retryRef = useRef(0);

    const connect = useCallback(() => {
        if (!user || !user.id) return;
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
                } catch { /* ignore parse errors */ }
            };
            ws.onclose = () => {
                setConnected(false);
                const delay = Math.min(15000, 1000 * Math.pow(2, retryRef.current++));
                setTimeout(connect, delay);
            };
            ws.onerror = () => { try { ws.close(); } catch { /* ignore */ } };
        } catch { /* will retry on close */ }
    }, [user]);

    useEffect(() => {
        if (!user || !user.id) return;
        connect();
        return () => { try { wsRef.current?.close(); } catch { /* ignore */ } };
    }, [user, connect]);

    return { lastEvent, connected };
}
