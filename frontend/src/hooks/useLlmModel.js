import { useEffect, useState } from "react";
import api from "@/lib/api";

let _cache = null;
let _inflight = null;

// #74 — live LLM model labels from the backend (tier → "Claude Sonnet 5.5"); falls back to "Claude" until loaded.
export function useLlmModel(tier = "analysis") {
    const [models, setModels] = useState(_cache);
    useEffect(() => {
        if (_cache) return undefined;
        let alive = true;
        _inflight = _inflight || api.get("/diagnostic/llm-models").then((r) => { _cache = r.data; return r.data; }).catch(() => null);
        _inflight.then((d) => { if (alive && d) setModels(d); });
        return () => { alive = false; };
    }, []);
    const pretty = models?.models_pretty?.[tier];
    return { label: pretty || "Claude", labelUpper: (pretty || "Claude").toUpperCase(), modelId: models?.models?.[tier] || null,
             provider: models?.provider || null, loaded: !!models };
}
