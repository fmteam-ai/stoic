import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { TOURS } from "@/lib/tours";
import { X } from "lucide-react";

const KEY = "stoic_active_tour";

export function startTour(id) {
    localStorage.setItem(KEY, JSON.stringify({ id, step: 0 }));
    window.dispatchEvent(new Event("stoic-tour"));
}

export function TourRunner() {
    const [state, setState] = useState(null);
    const [rect, setRect] = useState(null);
    const navigate = useNavigate();
    const location = useLocation();

    const read = useCallback(() => {
        try { setState(JSON.parse(localStorage.getItem(KEY))); }
        catch { setState(null); }
    }, []);

    useEffect(() => {
        read();
        window.addEventListener("stoic-tour", read);
        return () => window.removeEventListener("stoic-tour", read);
    }, [read]);

    const tour = state && TOURS[state.id];
    const step = tour?.steps[state.step];

    useEffect(() => {
        if (!step) { setRect(null); return; }
        if (step.route && location.pathname !== step.route) {
            navigate(step.route);
            return;
        }
        let tries = 0;
        const find = () => {
            const el = step.selector
                ? document.querySelector(`[data-testid="${step.selector}"]`)
                : null;
            if (el) {
                el.scrollIntoView({ block: "center", behavior: "smooth" });
                setTimeout(() => setRect(el.getBoundingClientRect()), 350);
            } else if (tries++ < 10) {
                setTimeout(find, 400);
            } else setRect(null);
        };
        find();
    }, [step, location.pathname, navigate]);

    if (!tour || !step) return null;

    const stop = () => { localStorage.removeItem(KEY); setState(null); setRect(null); };
    const go = (d) => {
        const n = state.step + d;
        if (n < 0) return;
        if (n >= tour.steps.length) { stop(); return; }
        localStorage.setItem(KEY, JSON.stringify({ id: state.id, step: n }));
        setRect(null);
        read();
    };

    const pad = 6;
    const box = rect && {
        top: rect.top - pad, left: rect.left - pad,
        width: rect.width + pad * 2, height: rect.height + pad * 2,
    };
    const tipTop = box ? Math.min(window.innerHeight - 190, box.top + box.height + 12) : window.innerHeight / 2 - 90;
    const tipLeft = box ? Math.min(Math.max(12, box.left), window.innerWidth - 360) : window.innerWidth / 2 - 170;

    return (
        <div className="fixed inset-0 z-[100]" data-testid="tour-overlay">
            <div className="absolute inset-0 bg-black/70" onClick={stop} />
            {box && (
                <div className="absolute border-2 border-[#00FF41] rounded-sm pointer-events-none transition-all duration-300"
                    data-testid="tour-highlight"
                    style={{ top: box.top, left: box.left, width: box.width, height: box.height, boxShadow: "0 0 0 9999px rgba(0,0,0,0.7), 0 0 24px rgba(0,255,65,0.35)" }} />
            )}
            <div className="absolute w-[340px] border border-[#1F1F1F] bg-[#0A0A0A] p-4 font-mono text-xs space-y-3"
                data-testid="tour-tooltip" style={{ top: tipTop, left: tipLeft }}>
                <div className="flex items-center justify-between">
                    <span className="uppercase tracking-widest text-[#00FF41]">{tour.title}</span>
                    <button onClick={stop} data-testid="tour-exit" className="text-[#52525B] hover:text-white"><X className="h-3.5 w-3.5" /></button>
                </div>
                <div className="text-[#E4E4E7] text-sm font-bold">{step.title}</div>
                <div className="text-[#A1A1AA] leading-relaxed">{step.body}</div>
                <div className="flex items-center justify-between pt-1">
                    <span className="text-[#52525B]">{state.step + 1} / {tour.steps.length}</span>
                    <div className="flex gap-2">
                        {state.step > 0 && (
                            <button onClick={() => go(-1)} data-testid="tour-back"
                                className="border border-[#1F1F1F] px-3 py-1 uppercase tracking-wider text-[#A1A1AA] hover:text-white">Back</button>
                        )}
                        <button onClick={() => go(1)} data-testid="tour-next"
                            className="border border-[#00FF41] px-3 py-1 uppercase tracking-wider text-[#00FF41] hover:bg-[#00FF41] hover:text-black transition-colors">
                            {state.step + 1 === tour.steps.length ? "Finish" : "Next"}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
}
